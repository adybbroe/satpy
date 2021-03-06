#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2010-2018 PyTroll Community

# Author(s):

#   Martin Raspaud <martin.raspaud@smhi.se>
#   Adam Bybbroe <adam.dybbroe@smhi.se>
#   Sauli Joro <sauli.joro@eumetsat.int>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""SEVIRI HRIT format reader
******************************

References:
    - MSG Level 1.5 Image Data Format Description
    - Radiometric Calibration of MSG SEVIRI Level 1.5 Image Data in Equivalent
      Spectral Blackbody Radiance


"""

import logging
from datetime import datetime

import numpy as np
import pyproj

from pyresample import geometry

from satpy.readers.eum_base import (time_cds_short,
                                    recarray2dict)
from satpy.readers.hrit_base import (HRITFileHandler, ancillary_text,
                                     annotation_header, base_hdr_map,
                                     image_data_function)

from satpy.readers.seviri_base import SEVIRICalibrationHandler, chebyshev
from satpy.readers.seviri_base import (CHANNEL_NAMES, VIS_CHANNELS, CALIB, SATNUM)

from satpy.readers.seviri_l1b_native_hdr import (hrit_prologue, hrit_epilogue,
                                                 impf_configuration)

logger = logging.getLogger('hrit_msg')

# MSG implementation:
key_header = np.dtype([('key_number', 'u1'),
                       ('seed', '>f8')])

segment_identification = np.dtype([('GP_SC_ID', '>i2'),
                                   ('spectral_channel_id', '>i1'),
                                   ('segment_sequence_number', '>u2'),
                                   ('planned_start_segment_number', '>u2'),
                                   ('planned_end_segment_number', '>u2'),
                                   ('data_field_representation', '>i1')])

image_segment_line_quality = np.dtype([('line_number_in_grid', '>i4'),
                                       ('line_mean_acquisition',
                                        [('days', '>u2'),
                                         ('milliseconds', '>u4')]),
                                       ('line_validity', 'u1'),
                                       ('line_radiometric_quality', 'u1'),
                                       ('line_geometric_quality', 'u1')])

msg_variable_length_headers = {
    image_segment_line_quality: 'image_segment_line_quality'}

msg_text_headers = {image_data_function: 'image_data_function',
                    annotation_header: 'annotation_header',
                    ancillary_text: 'ancillary_text'}

msg_hdr_map = base_hdr_map.copy()
msg_hdr_map.update({7: key_header,
                    128: segment_identification,
                    129: image_segment_line_quality
                    })


orbit_coef = np.dtype([('StartTime', time_cds_short),
                       ('EndTime', time_cds_short),
                       ('X', '>f8', (8, )),
                       ('Y', '>f8', (8, )),
                       ('Z', '>f8', (8, )),
                       ('VX', '>f8', (8, )),
                       ('VY', '>f8', (8, )),
                       ('VZ', '>f8', (8, ))])

attitude_coef = np.dtype([('StartTime', time_cds_short),
                          ('EndTime', time_cds_short),
                          ('XofSpinAxis', '>f8', (8, )),
                          ('YofSpinAxis', '>f8', (8, )),
                          ('ZofSpinAxis', '>f8', (8, ))])

cuc_time = np.dtype([('coarse', 'u1', (4, )),
                     ('fine', 'u1', (3, ))])


class NoValidNavigationCoefs(Exception):
    pass


class HRITMSGPrologueFileHandler(HRITFileHandler):
    """SEVIRI HRIT prologue reader.
    """

    def __init__(self, filename, filename_info, filetype_info, calib_mode='nominal',
                 ext_calib_coefs=None):
        """Initialize the reader."""
        super(HRITMSGPrologueFileHandler, self).__init__(filename, filename_info,
                                                         filetype_info,
                                                         (msg_hdr_map,
                                                          msg_variable_length_headers,
                                                          msg_text_headers))
        self.prologue = {}
        self.read_prologue()
        self.satpos = None

        service = filename_info['service']
        if service == '':
            self.mda['service'] = '0DEG'
        else:
            self.mda['service'] = service

    def read_prologue(self):
        """Read the prologue metadata."""

        with open(self.filename, "rb") as fp_:
            fp_.seek(self.mda['total_header_length'])
            data = np.fromfile(fp_, dtype=hrit_prologue, count=1)
            self.prologue.update(recarray2dict(data))
            try:
                impf = np.fromfile(fp_, dtype=impf_configuration, count=1)[0]
            except IndexError:
                logger.info('No IMPF configuration field found in prologue.')
            else:
                self.prologue.update(recarray2dict(impf))

    def get_satpos(self):
        """Get actual satellite position in geodetic coordinates (WGS-84)

        Returns: Longitude [deg east], Latitude [deg north] and Altitude [m]
        """
        if self.satpos is None:
            logger.debug("Computing actual satellite position")

            try:
                # Get satellite position in cartesian coordinates
                x, y, z = self._get_satpos_cart()

                # Transform to geodetic coordinates
                geocent = pyproj.Proj(proj='geocent')
                a, b = self.get_earth_radii()
                latlong = pyproj.Proj(proj='latlong', a=a, b=b, units='m')
                lon, lat, alt = pyproj.transform(geocent, latlong, x, y, z)
            except NoValidNavigationCoefs as err:
                logger.warning(err)
                lon = lat = alt = None

            # Cache results
            self.satpos = lon, lat, alt

        return self.satpos

    def _get_satpos_cart(self):
        """Determine satellite position in earth-centered cartesion coordinates

        The coordinates as a function of time are encoded in the coefficients of an 8th-order Chebyshev polynomial.
        In the prologue there is one set of coefficients for each coordinate (x, y, z). The coordinates are obtained by
        evalutaing the polynomials at the start time of the scan.

        Returns: x, y, z [m]
        """
        orbit_polynomial = self.prologue['SatelliteStatus']['Orbit']['OrbitPolynomial']

        # Find Chebyshev coefficients for the given time
        coef_idx = self._find_navigation_coefs()
        tstart = orbit_polynomial['StartTime'][0, coef_idx]
        tend = orbit_polynomial['EndTime'][0, coef_idx]

        # Obtain cartesian coordinates (x, y, z) of the satellite by evaluating the Chebyshev polynomial at the
        # start time of the scan. Express timestamps in microseconds since 1970-01-01 00:00.
        time = self.prologue['ImageAcquisition']['PlannedAcquisitionTime']['TrueRepeatCycleStart']
        time64 = np.datetime64(time).astype('int64')
        domain = [np.datetime64(tstart).astype('int64'),
                  np.datetime64(tend).astype('int64')]
        x = chebyshev(coefs=orbit_polynomial['X'][coef_idx], time=time64, domain=domain)
        y = chebyshev(coefs=orbit_polynomial['Y'][coef_idx], time=time64, domain=domain)
        z = chebyshev(coefs=orbit_polynomial['Z'][coef_idx], time=time64, domain=domain)

        return x*1000, y*1000, z*1000  # km -> m

    def _find_navigation_coefs(self):
        """Find navigation coefficients for the current time

        The navigation Chebyshev coefficients are only valid for a certain time interval. The header entry
        SatelliteStatus/Orbit/OrbitPolynomial contains multiple coefficients for multiple time intervals. Find the
        coefficients which are valid for the nominal timestamp of the scan.

        Returns: Corresponding index in the coefficient list.
        """
        # Find index of interval enclosing the nominal timestamp of the scan
        time = np.datetime64(self.prologue['ImageAcquisition']['PlannedAcquisitionTime']['TrueRepeatCycleStart'])
        intervals_tstart = self.prologue['SatelliteStatus']['Orbit']['OrbitPolynomial']['StartTime'][0].astype(
            'datetime64[us]')
        intervals_tend = self.prologue['SatelliteStatus']['Orbit']['OrbitPolynomial']['EndTime'][0].astype(
            'datetime64[us]')
        try:
            return np.where(np.logical_and(time >= intervals_tstart, time < intervals_tend))[0][0]
        except IndexError:
            raise NoValidNavigationCoefs('Unable to find navigation coefficients valid for {}'.format(time))

    def get_earth_radii(self):
        """Get earth radii from prologue

        Returns:
            Equatorial radius, polar radius [m]
        """
        earth_model = self.prologue['GeometricProcessing']['EarthModel']
        a = earth_model['EquatorialRadius'] * 1000
        b = (earth_model['NorthPolarRadius'] +
             earth_model['SouthPolarRadius']) / 2.0 * 1000
        return a, b


class HRITMSGEpilogueFileHandler(HRITFileHandler):
    """SEVIRI HRIT epilogue reader.
    """

    def __init__(self, filename, filename_info, filetype_info, calib_mode='nominal',
                 ext_calib_coefs=None):
        """Initialize the reader."""
        super(HRITMSGEpilogueFileHandler, self).__init__(filename, filename_info,
                                                         filetype_info,
                                                         (msg_hdr_map,
                                                          msg_variable_length_headers,
                                                          msg_text_headers))
        self.epilogue = {}
        self.read_epilogue()

        service = filename_info['service']
        if service == '':
            self.mda['service'] = '0DEG'
        else:
            self.mda['service'] = service

    def read_epilogue(self):
        """Read the epilogue metadata."""

        with open(self.filename, "rb") as fp_:
            fp_.seek(self.mda['total_header_length'])
            data = np.fromfile(fp_, dtype=hrit_epilogue, count=1)
            self.epilogue.update(recarray2dict(data))


class HRITMSGFileHandler(HRITFileHandler, SEVIRICalibrationHandler):
    """SEVIRI HRIT format reader

    It is possible to choose between two file-internal calibration coefficients for the conversion
    from counts to radiances:

        - Nominal for all channels (default)
        - GSICS for IR channels and nominal for VIS channels

    In order to change the default behaviour, use the ``reader_kwargs`` upon Scene creation::

        import satpy
        import glob

        filenames = glob.glob('H-000-MSG3*')
        scene = satpy.Scene(filenames,
                            reader='seviri_l1b_hrit',
                            reader_kwargs={'calib_mode': 'GSICS'})
        scene.load(['VIS006', 'IR_108'])

    Furthermore, it is possible to specify external calibration coefficients for the conversion from
    counts to radiances. They must be specified in [mW m-2 sr-1 (cm-1)-1]. External coefficients
    take precedence over internal coefficients. If external calibration coefficients are specified
    for only a subset of channels, the remaining channels will be calibrated using the chosen
    file-internal coefficients (nominal or GSICS).

    In the following example we use external calibration coefficients for the ``VIS006`` &
    ``IR_108`` channels, and nominal coefficients for the remaining channels::

        coefs = {'VIS006': {'gain': 0.0236, 'offset': -1.20},
                 'IR_108': {'gain': 0.2156, 'offset': -10.4}}
        scene = satpy.Scene(filenames,
                            reader='seviri_l1b_hrit',
                            reader_kwargs={'ext_calib_coefs': coefs})
        scene.load(['VIS006', 'VIS008', 'IR_108', 'IR_120'])

    In the next example we use we use external calibration coefficients for the ``VIS006`` &
    ``IR_108`` channels, nominal coefficients for the remaining VIS channels and GSICS coefficients
    for the remaining IR channels::

        coefs = {'VIS006': {'gain': 0.0236, 'offset': -1.20},
                 'IR_108': {'gain': 0.2156, 'offset': -10.4}}
        scene = satpy.Scene(filenames,
                            reader='seviri_l1b_hrit',
                            reader_kwargs={'calib_mode': 'GSICS',
                                           'ext_calib_coefs': coefs})
        scene.load(['VIS006', 'VIS008', 'IR_108', 'IR_120'])

    """
    def __init__(self, filename, filename_info, filetype_info,
                 prologue, epilogue, calib_mode='nominal', ext_calib_coefs=None):
        """Initialize the reader."""
        super(HRITMSGFileHandler, self).__init__(filename, filename_info,
                                                 filetype_info,
                                                 (msg_hdr_map,
                                                  msg_variable_length_headers,
                                                  msg_text_headers))

        self.prologue_ = prologue
        self.prologue = prologue.prologue
        self.epilogue = epilogue.epilogue
        self._filename_info = filename_info
        self.ext_calib_coefs = ext_calib_coefs if ext_calib_coefs is not None else {}
        calib_mode_choices = ('NOMINAL', 'GSICS')
        if calib_mode.upper() not in calib_mode_choices:
            raise ValueError('Invalid calibration mode: {}. Choose one of {}'.format(
                calib_mode, calib_mode_choices))
        self.calib_mode = calib_mode.upper()

        self._get_header()

    def _get_header(self):
        """Read the header info, and fill the metadata dictionary"""

        earth_model = self.prologue['GeometricProcessing']['EarthModel']
        self.mda['offset_corrected'] = earth_model['TypeOfEarthModel'] == 2

        # Projection
        a, b = self.prologue_.get_earth_radii()
        self.mda['projection_parameters']['a'] = a
        self.mda['projection_parameters']['b'] = b
        ssp = self.prologue['ImageDescription'][
            'ProjectionDescription']['LongitudeOfSSP']
        self.mda['projection_parameters']['SSP_longitude'] = ssp
        self.mda['projection_parameters']['SSP_latitude'] = 0.0

        # Navigation
        actual_lon, actual_lat, actual_alt = self.prologue_.get_satpos()
        self.mda['navigation_parameters']['satellite_nominal_longitude'] = self.prologue['SatelliteStatus'][
            'SatelliteDefinition']['NominalLongitude']
        self.mda['navigation_parameters']['satellite_nominal_latitude'] = 0.0
        self.mda['navigation_parameters']['satellite_actual_longitude'] = actual_lon
        self.mda['navigation_parameters']['satellite_actual_latitude'] = actual_lat
        self.mda['navigation_parameters']['satellite_actual_altitude'] = actual_alt

        # Misc
        self.platform_id = self.prologue["SatelliteStatus"][
            "SatelliteDefinition"]["SatelliteId"]
        self.platform_name = "Meteosat-" + SATNUM[self.platform_id]
        self.mda['platform_name'] = self.platform_name
        service = self._filename_info['service']
        if service == '':
            self.mda['service'] = '0DEG'
        else:
            self.mda['service'] = service
        self.channel_name = CHANNEL_NAMES[self.mda['spectral_channel_id']]

    @property
    def start_time(self):

        return self.epilogue['ImageProductionStats'][
            'ActualScanningSummary']['ForwardScanStart']

    @property
    def end_time(self):

        return self.epilogue['ImageProductionStats'][
            'ActualScanningSummary']['ForwardScanEnd']

    def get_xy_from_linecol(self, line, col, offsets, factors):
        """Get the intermediate coordinates from line & col.

        Intermediate coordinates are actually the instruments scanning angles.
        """
        loff, coff = offsets
        lfac, cfac = factors
        x__ = (col - coff) / cfac * 2**16
        y__ = - (line - loff) / lfac * 2**16

        return x__, y__

    def get_area_extent(self, size, offsets, factors, platform_height):
        """Get the area extent of the file.

        Until December 2017, the data is shifted by 1.5km SSP North and West against the nominal GEOS projection. Since
        December 2017 this offset has been corrected. A flag in the data indicates if the correction has been applied.
        If no correction was applied, adjust the area extent to match the shifted data.

        For more information see Section 3.1.4.2 in the MSG Level 1.5 Image Data Format Description. The correction
        of the area extent is documented in a `developer's memo <https://github.com/pytroll/satpy/wiki/
        SEVIRI-georeferencing-offset-correction>`_.
        """
        nlines, ncols = size
        h = platform_height

        loff, coff = offsets
        loff -= nlines
        offsets = loff, coff
        # count starts at 1
        cols = 1 - 0.5
        lines = 0.5 - 1
        ll_x, ll_y = self.get_xy_from_linecol(-lines, cols, offsets, factors)

        cols += ncols
        lines += nlines
        ur_x, ur_y = self.get_xy_from_linecol(-lines, cols, offsets, factors)

        aex = (np.deg2rad(ll_x) * h, np.deg2rad(ll_y) * h,
               np.deg2rad(ur_x) * h, np.deg2rad(ur_y) * h)

        if not self.mda['offset_corrected']:
            # Geo-referencing offset present. Adjust area extent to match the shifted data. Note that we have to adjust
            # the corners in the *opposite* direction, i.e. S-E. Think of it as if the coastlines were fixed and you
            # dragged the image to S-E until coastlines and data area aligned correctly.
            #
            # Although the image is flipped upside-down and left-right, the projection coordinates retain their
            # properties, i.e. positive x/y is East/North, respectively.
            xadj = 1500
            yadj = -1500
            aex = (aex[0] + xadj, aex[1] + yadj,
                   aex[2] + xadj, aex[3] + yadj)

        return aex

    def get_area_def(self, dsid):
        """Get the area definition of the band."""
        if dsid.name != 'HRV':
            return super(HRITMSGFileHandler, self).get_area_def(dsid)

        cfac = np.int32(self.mda['cfac'])
        lfac = np.int32(self.mda['lfac'])
        loff = np.float32(self.mda['loff'])

        a = self.mda['projection_parameters']['a']
        b = self.mda['projection_parameters']['b']
        h = self.mda['projection_parameters']['h']
        lon_0 = self.mda['projection_parameters']['SSP_longitude']

        nlines = int(self.mda['number_of_lines'])
        ncols = int(self.mda['number_of_columns'])

        segment_number = self.mda['segment_sequence_number']

        current_first_line = (segment_number -
                              self.mda['planned_start_segment_number']) * nlines
        bounds = self.epilogue['ImageProductionStats']['ActualL15CoverageHRV']

        upper_south_line = bounds[
            'LowerNorthLineActual'] - current_first_line - 1
        upper_south_line = min(max(upper_south_line, 0), nlines)

        lower_coff = (5566 - bounds['LowerEastColumnActual'] + 1)
        upper_coff = (5566 - bounds['UpperEastColumnActual'] + 1)

        lower_area_extent = self.get_area_extent((upper_south_line, ncols),
                                                 (loff, lower_coff),
                                                 (lfac, cfac),
                                                 h)

        upper_area_extent = self.get_area_extent((nlines - upper_south_line,
                                                  ncols),
                                                 (loff - upper_south_line,
                                                  upper_coff),
                                                 (lfac, cfac),
                                                 h)

        proj_dict = {'a': float(a),
                     'b': float(b),
                     'lon_0': float(lon_0),
                     'h': float(h),
                     'proj': 'geos',
                     'units': 'm'}

        lower_area = geometry.AreaDefinition(
            'some_area_name',
            "On-the-fly area",
            'geosmsg',
            proj_dict,
            ncols,
            upper_south_line,
            lower_area_extent)

        upper_area = geometry.AreaDefinition(
            'some_area_name',
            "On-the-fly area",
            'geosmsg',
            proj_dict,
            ncols,
            nlines - upper_south_line,
            upper_area_extent)

        area = geometry.StackedAreaDefinition(lower_area, upper_area)

        self.area = area.squeeze()
        return area

    def get_dataset(self, key, info):
        res = super(HRITMSGFileHandler, self).get_dataset(key, info)
        res = self.calibrate(res, key.calibration)
        res.attrs['units'] = info['units']
        res.attrs['wavelength'] = info['wavelength']
        res.attrs['standard_name'] = info['standard_name']
        res.attrs['platform_name'] = self.platform_name
        res.attrs['sensor'] = 'seviri'
        res.attrs['satellite_longitude'] = self.mda[
            'projection_parameters']['SSP_longitude']
        res.attrs['satellite_latitude'] = self.mda[
            'projection_parameters']['SSP_latitude']
        res.attrs['satellite_altitude'] = self.mda['projection_parameters']['h']
        res.attrs['projection'] = {'satellite_longitude': self.mda['projection_parameters']['SSP_longitude'],
                                   'satellite_latitude': self.mda['projection_parameters']['SSP_latitude'],
                                   'satellite_altitude': self.mda['projection_parameters']['h']}
        res.attrs['navigation'] = self.mda['navigation_parameters'].copy()
        res.attrs['georef_offset_corrected'] = self.mda['offset_corrected']

        return res

    def calibrate(self, data, calibration):
        """Calibrate the data."""
        tic = datetime.now()
        channel_name = self.channel_name

        if calibration == 'counts':
            res = data
        elif calibration in ['radiance', 'reflectance', 'brightness_temperature']:
            # Choose calibration coefficients
            # a) Internal: Nominal or GSICS?
            band_idx = self.mda['spectral_channel_id'] - 1
            if self.calib_mode != 'GSICS' or self.channel_name in VIS_CHANNELS:
                # you cant apply GSICS values to the VIS channels
                coefs = self.prologue["RadiometricProcessing"]["Level15ImageCalibration"]
                int_gain = coefs['CalSlope'][band_idx]
                int_offset = coefs['CalOffset'][band_idx]
            else:
                coefs = self.prologue["RadiometricProcessing"]['MPEFCalFeedback']
                int_gain = coefs['GSICSCalCoeff'][band_idx]
                int_offset = coefs['GSICSOffsetCount'][band_idx]

            # b) Internal or external? External takes precedence.
            gain = self.ext_calib_coefs.get(self.channel_name, {}).get('gain', int_gain)
            offset = self.ext_calib_coefs.get(self.channel_name, {}).get('offset', int_offset)

            # Convert to radiance
            data = data.where(data > 0)
            res = self._convert_to_radiance(data.astype(np.float32), gain, offset)
            line_mask = self.mda['image_segment_line_quality']['line_validity'] >= 2
            line_mask &= self.mda['image_segment_line_quality']['line_validity'] <= 3
            line_mask &= self.mda['image_segment_line_quality']['line_radiometric_quality'] == 4
            line_mask &= self.mda['image_segment_line_quality']['line_geometric_quality'] == 4
            res *= np.choose(line_mask, [1, np.nan])[:, np.newaxis].astype(np.float32)

        if calibration == 'reflectance':
            solar_irradiance = CALIB[self.platform_id][channel_name]["F"]
            res = self._vis_calibrate(res, solar_irradiance)

        elif calibration == 'brightness_temperature':
            cal_type = self.prologue['ImageDescription'][
                'Level15ImageProduction']['PlannedChanProcessing'][self.mda['spectral_channel_id']]
            res = self._ir_calibrate(res, channel_name, cal_type)

        logger.debug("Calibration time " + str(datetime.now() - tic))
        return res


def show(data, negate=False):
    """Show the stretched data.
    """
    from PIL import Image as pil
    data = np.array((data - data.min()) * 255.0 /
                    (data.max() - data.min()), np.uint8)
    if negate:
        data = 255 - data
    img = pil.fromarray(data)
    img.show()
