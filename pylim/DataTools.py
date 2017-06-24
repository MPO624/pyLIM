"""
Module for data interaction tools for the LIM package.

Author: Andre Perkins
"""

import tables as tb
import dask
import dask.array as da
import numpy as np
import os.path as path
import netCDF4 as ncf
import numexpr as ne
import chest
import cPickle as cpk
import logging

from Stats import run_mean, calc_anomaly, detrend_data, is_dask_array, \
                  dask_detrend_data, calc_eofs

# Prevents any nodes in HDF5 file from being cached, saving space
tb.parameters.NODE_CACHE_SLOTS = 0

# Set the overflow cache for Dask operations
CACHE = chest.Chest(available_memory=30e9,
                    path='/home/katabatic/wperkins/scratch')
dask.set_options(cache=CACHE)

# Initialize logging client for this module
logger = logging.getLogger(__name__)


class BaseDataObject(object):
    """Data Input Object

    This class is for handling data which may be in a masked format. This
    class can also be used to expand previously compressed data if an
    original mask is provided.


    Notes
    -----
    Right now it is writen to work with 2D spatial data. It assumes that
    the leading dimension is temporal. In the future it might change to
    incorporate 3D spatial fields or just general data.
    """

    # Static names
    TIME = 'time'
    LEVEL = 'level'
    LAT = 'lat'
    LON = 'lon'
    EQUIVLAT = 'equiv_lat'

    # Static databin keys
    _COMPRESSED = 'compressed_data'
    _ORIGDATA = 'orig'
    _DETRENDED = 'detrended'
    _AWGHT = 'area_weighted'
    _RUNMEAN = 'running_mean'
    _ANOMALY = 'anomaly'
    _CLIMO = 'climo'
    _EOFPROJ = 'eof_proj'

    @staticmethod
    def _match_dims(shape, dim_coords):
        """
        Match each dimension key in dim_coords dict to the correct index of the
        shape.
        """
        return {key: value[0] for key, value in dim_coords.items()
                if shape[value[0]] == len(value[1])}

    def __init__(self, data, dim_coords=None, coord_grids=None,
                 valid_data=None, force_flat=False,
                 save_none=False, time_units=None, time_cal=None,
                 fill_value=None, rotated_pole=False):
        """
        Construction of a DataObject from input data.  If nan or
        infinite values are present, a compressed version of the data
        is stored.

        Parameters
        ----------
        data: ndarray
            Input dataset to be used.
        dim_coords: dict(str:(int, ndarray)
            Dimension position and oordinate vector dictionary for supplied
            data.  Please use DataObject attributes (e.g. DataObject.TIME)
            for dictionary keys.
        coord_grids: dict(str:ndarray), optional
            Full grids of each dimensions coordinates.  If not provided these
            can be created from dim_coords as long as the grid is regular.
            If grid is irregular these should be provided for easier plotting.
        valid_data: ndarray (np.bool), optional
            Array corresponding to valid data in the of the input dataset
            or uncompressed version of the input dataset.  Should have the same
            number of dimensions as the data and each  dimension should be
            greater than or equal to the spatial dimensions of data.
        force_flat: bool, optional
            Force spatial dimensions to be flattened (1D array)
        save_none: bool, optional
            If true, data object will not save any of the intermediate
            calculation data.
        time_units: str, optional
            Units string to be used by netcdf.date2num function for storing
            datetime objects as a numeric value for output.
        time_cal: str, optional
            Calendar string to be used by netcdf.date2num function for storing
            datetime objects as a numeric value for output
        fill_value: float
            Value to be considered invalid data during the mask and 
            compression. Only considered when data is not masked.
        rotated_pole: bool, optional
            Whether or not the grid is on a rotated pole grid
        """

        logger.info('Initializing data object from {}'.format(self.__class__))

        assert data.ndim <= 4, 'Maximum of 4 dimensions are allowed.'
        self._full_shp = data.shape
        self.data_dtype = data.dtype
        self.forced_flat = force_flat
        self.time_units = time_units
        self.time_cal = time_cal
        self._coord_grids = coord_grids
        self._rotated_pole = rotated_pole
        self._fill_value = fill_value
        self._save_none = save_none
        self._data_bins = {}
        self._curr_data_key = None
        self._ops_performed = []
        self._start_time_edge = None
        self._end_time_edge = None
        self._eofs = None
        self._svals = None
        self._eof_stats = {}
        self._tb_file_args = None

        # Future possible data manipulation functionality
        self.anomaly = None
        self.climo = None
        self.compressed_data = None
        self.running_mean = None
        self.detrended = None
        self.area_weighted = None
        self.eof_proj = None

        # Match dimension coordinate vectors
        if dim_coords is not None:
            if self.TIME in dim_coords.keys():
                if dim_coords[self.TIME][0] != 0:
                    logger.error('Non-leading time dimension encountered in '
                                 'dim_coords.')
                    raise ValueError('Sampling dimension must always be the '
                                     'leading dimension if provided.')

                self._leading_time = True
                self._time_shp = [data.shape[0]]
                self._spatial_shp = data.shape[1:]
            else:
                self._leading_time = False
                self._time_shp = []
                self._spatial_shp = self._full_shp
            self._dim_idx = self._match_dims(data.shape, dim_coords)
            self._dim_coords = dim_coords
        else:
            self._leading_time = False
            self._time_shp = []
            self._spatial_shp = self._full_shp
            self._dim_idx = None

        # Rough method to provide lats for area weighting
        if rotated_pole:
            lat_idx = self._dim_idx[self.LAT]
            lat = self._dim_coords.get(self.LAT, None)
            if lat is not None:
                nlats = self._full_shp[lat_idx]
                bnds = np.linspace(-90, 90, nlats+1)
                centers = (bnds[:-1] + bnds[1:]) / 2.
                equiv_dim_coord = (lat_idx, centers)
                self._dim_coords[self.EQUIVLAT] = equiv_dim_coord
                self._dim_idx[self.EQUIVLAT] = lat_idx

        self._flat_spatial_shp = [np.product(self._spatial_shp)]

        logger.info('Time shape: {}'.format(self._time_shp))
        logger.info('Spatial shape: {}\n'.format(self._spatial_shp))
        logger.info('Flattened spatial length: '
                    '{}'.format(self._flat_spatial_shp))

        # Check to see if data input is a compressed version
        compressed = False
        if valid_data is not None:
            dim_lim = valid_data.ndim

            if dim_lim <= 3:
                logger.error('Valid data has more than 3 dimensions: '
                             'ndim={}'.format())
                raise ValueError('Valid data mask should not have more than 3 '
                                 'dimensions')
            elif dim_lim != len(self._spatial_shp):
                logger.error('Valid data dimensions not equivalent to the '
                             'shape of the spatial field: \n'
                             'valid_data.ndim={}\n'
                             '_spatial_shp.ndim={}'.format(dim_lim,
                                                           self._spatial_shp))

            # Check the dimensions of the mask and data to se if compressed
            for dat_dim, mask_dim in zip(self._spatial_shp, valid_data.shape):
                if dat_dim > mask_dim:
                    logger.error('Data dimension greater than mask dimension:'
                                 '{} > {}'.format(dat_dim, mask_dim))
                    raise ValueError('Encountered data dimension larger than'
                                     'equivalent masked dimension.')

                compressed |= dat_dim < mask_dim

            # Apply input mask if its spatial dimensions match data
            if not compressed:
                # multplication broadcasts across leading sampling dimension if
                # applicable
                full_valid = np.ones_like(data, dtype=np.bool) * valid_data
                data[~full_valid] = np.nan
                logger.debug('Mask applied (NaN) to non-compressed data.')
            else:
                if not np.all(np.isfinite(data)):
                    logger.error('Data determined to be compressed still '
                                 'contains non-finite elements.')
                    raise ValueError('Non-finite value encountered in '
                                     'compressed data.')

                self._full_shp = self._time_shp + list(valid_data.shape)
                logger.debug('Compressed data encountered. Full shape: '
                             '{}'.format(self._full_shp))

            self.valid_data = valid_data.flatten()
            self.is_masked = True

        # Masked array valid handling
        self.is_masked, self.valid_data = self._data_masking(data)
        if self.valid_data is not None:
            self.valid_data = self.valid_data.flatten()

        self.data = data
        # Flatten Spatial Dimension if applicable
        if force_flat or self.is_masked:
            self._flatten_curr_data()
            logger.debug('Flattening data over spatial dimensions. New shp: '
                         '{}'.format(self.data.shape))

        self._set_curr_data_key(self._ORIGDATA)

        # Compress the data if mask is present
        if compressed:
            self.compressed_data = self.data
        elif self.is_masked:
            if not save_none:
                if self._leading_time:
                    new_shp = (self._time_shp[0], self.valid_data.sum())
                else:
                    new_shp = (self.valid_data.sum(),)
                self.compressed_data = self._new_empty_databin(new_shp,
                                                               self.data.dtype,
                                                               self._COMPRESSED)

            self.data = self._compress_to_valid_data(self.data,
                                                     self.valid_data,
                                                     out_arr=self.compressed_data)
            self._set_curr_data_key(self._COMPRESSED)

    def _set_curr_data_key(self, key):
        logger.debug('Setting current data key to: '.format(key))
        self._curr_data_key = key

    def _new_empty_databin(self, shape, dtype, name):
        """
        Create an empty backend data container.
        """
        logger.debug('Creating empty databin: \n'
                     'shape: {}\n'
                     'dtype: {}\n'
                     'name: {}'.format(shape, dtype, name))

        new = np.empty(shape, dtype=dtype)
        self._data_bins[name] = new
        return new

    def _new_databin(self, data, name):
        """
        Create and copy data into a new backend data container.
        """
        logger.debug('Copying data to databin: {}'.format(name))
        new = np.empty_like(data)
        new[:] = data
        self._data_bins[name] = new
        return new

    def _gen_composite_mask(self, data):
        """
        Generate a mask (over the time dimension if present) that masks all
        locations that are missing data.
        """
        logger.debug('Generating composite mask from data mask.')
        if self._leading_time:
            composite_mask = data.mask.sum(axis=0) > 0
        else:
            composite_mask = data.mask

        return composite_mask

    def _check_invalid_data(self, data):
        """
        Check for invalid (inf or NaN) data in the data.  Like
        _gen_composite_mask it operates over the time dimension if present,
        and only returns true for locations that have all valid data.
        """
        logger.info('Checking data for invalid elements.')

        full_valid = np.isfinite(data)
        if self._fill_value is not None:
            full_valid &= data != self._fill_value

        if not np.all(full_valid):
            masked = True
            if self._leading_time:
                valid_data = full_valid.sum() < self._time_shp[0]
            else:
                valid_data = full_valid

            logger.debug('Found invalid values. {:d} spatial elements masked.'
                         ''.format(np.logical_not(valid_data).sum()))
        else:
            logger.debug('No invalid values encountered.')
            masked = False
            valid_data = None

        return masked, valid_data

    def _data_masking(self, data):
        """
        Check and generate a valid data mask.
        """
        logger.info('Performing masking and invalid data checks.')
        if np.ma.is_masked(data[0]):
            masked = True
            composite_mask = self._gen_composite_mask(data)
            valid_data = np.logical_not(composite_mask)
        else:
            masked, valid_data = self._check_invalid_data(data)

        return masked, valid_data

    def _compress_to_valid_data(self, data, valid_mask, out_arr=None):
        """
        Compress data to only the valid locations.
        """
        logger.info('Compressing data to valid spatial locations.')
        if self._leading_time:
            compress_axis = 1
        else:
            compress_axis = None

        out_arr = np.compress(valid_mask, data, axis=compress_axis, out=out_arr)
        return out_arr

    def _flatten_curr_data(self):
        """
        Flatten the spatial dimension of data pointed to by self.data
        """
        if self._leading_time:
            self.data = self.data.reshape(self._time_shp + self._flat_spatial_shp)
        else:
            self.data = self.data.reshape(self._flat_spatial_shp)

    @staticmethod
    def _detrend_func(data, output_arr=None):
        return detrend_data(data, output_arr=output_arr)

    def inflate_full_grid(self, data=None, expand_axis=-1, reshape_orig=False):
        """
        Returns previously compressed data to its full grid filled with np.NaN
        values.

        Parameters
        ----------
        data: ndarray like, optional
            Data to inflate to its original grid size. If none specified this
            operates on the current data pointed to by self.data.
        expand_axis: int, optional
            Which axis to expand along for the data. Defaults to -1 which is
            the correct axis when operating on self.data.
        reshape_orig: bool, optional
            If true it will reshape data to the correct time shape (if
            applicable) and spatial shape.

        Returns
        -------
        ndarray
            Full decompressed grid filled with NaN values in masked locations.
        """
        if not self.is_masked:
            logger.warning('Cannot inflate uncompressed data.')
            return None

        if data is not None:
            # Check that this data was compressed from current object
            elem_expand_axis = data.shape[expand_axis]
            num_valid_points = self.valid_data.sum()
            if elem_expand_axis != num_valid_points:
                logger.error('Incorrect number of elements for compressed '
                             'data associated with this object.\n'
                             'data.shape=[{:d}]\n'
                             'nelem valid data={:d}'
                             ''.format(elem_expand_axis, num_valid_points))
                raise ValueError('Input data does not have same length as '
                                 'number of valid data elements.')

            shp = list(data.shape)
            shp[expand_axis] = len(self.valid_data)

        else:
            data = self.data
            shp = self._time_shp + [len(self.valid_data)]

        full = np.empty(shp) * np.nan
        valid_mask = self.valid_data
        for dim_idx, dim_len in enumerate(shp):
            if dim_len != self.valid_data.shape[0]:
                valid_mask = np.expand_dims(valid_mask, dim_idx)
        valid_mask = np.logical_and(np.ones(shp), valid_mask)
        print valid_mask.sum()
        full[valid_mask] = data.flatten()

        if reshape_orig:
            new_shp = list(shp)

            new_shp.pop(expand_axis)
            for dim_len in self._spatial_shp[::-1]:
                new_shp.insert(expand_axis, dim_len)
            full = full.reshape(new_shp)

        logger.debug('Inflated grid shape: {}'.format(full.shape))

        return full

    def calc_running_mean(self, window_size, year_len, save=True):
        """
        Calculate a running mean over the sampling dimension.

        Parameters
        ----------
        window_size: int
            Number of samples to include in the running mean window.
        year_len: int
            Number of samples in a year.  If sampling frequency is longer
            than 1 year this will default to 1.
        save: bool, optional
            Whether or not to save data in a new databin. (Default is True)

        Returns
        -------
        ndarray-like
            Data filtered using a running mean.

        Notes
        -----
        This function will trim each end of the sample by removing
        ciel(window_size//2 / year_len) * year_len.
        """
        logger.info('Filtering data using running mean...')
        logger.debug('window_size = {:d}, year_len = {:d}'.format(window_size,
                                                                  year_len))

        # TODO: year_len should eventually be a property determined during init
        if not self._leading_time:
            logger.error('Running mean requires leading time dimension.')
            raise ValueError('Can only perform a running mean when data has a '
                             'leading sampling dimension.')

        if year_len < 1:
            year_len = 1

        edge_pad = window_size // 2
        edge_trim = np.ceil(edge_pad / float(year_len)) * year_len

        new_time = self.data.shape[0] - edge_trim * 2
        if save and not self._save_none:
            new_shape = list(self.data.shape)
            new_shape[0] = new_time
            new_shape = tuple(new_shape)
            self.running_mean = self._new_empty_databin(new_shape,
                                                        self.data.dtype,
                                                        self._RUNMEAN)

        self.data = run_mean(self.data, window_size, trim_edge=edge_trim,
                             output_arr=self.running_mean)
        self._time_shp = [new_time]
        self._start_time_edge = edge_trim
        self._end_time_edge = -edge_trim
        self._set_curr_data_key(self._RUNMEAN)

        return self.data

    # TODO: Use provided time coordinates to determine year size
    def calc_anomaly(self, year_len, save=True, climo=None):
        """
        Center the data (anomaly) over the sampling dimension. If the there are
        multiple samples within a year (yr_size>1) then the climatology is
        calculated for each subannual quantity.

        Parameters
        ----------
        year_len: int
            Number of samples in a year.  If sampling frequency is longer
            than 1 year this will default to 1.
        save: bool, optional
            Whether or not to save data in a new databin. (Default is True)

        Returns
        -------
        ndarray-like
            Centered data
        """
        logger.info('Centering data and saving climatology...')
        logger.debug('yr_size = {:d}'.format(year_len))
        if not self._leading_time:
            raise ValueError('Can only perform anomaly calculation with a '
                             'specified leading sampling dimension')

        if save and not self._save_none:
            self.anomaly = self._new_empty_databin(self.data.shape,
                                                   self.data.dtype,
                                                   self._ANOMALY)

        if year_len < 1:
            year_len = 1

        self.data, self.climo = calc_anomaly(self.data, year_len,
                                             climo=climo,
                                             output_arr=self.anomaly)

        self._set_curr_data_key(self._ANOMALY)
        return self.data

    def detrend_data(self, save=True):
        """
        Remove linear trends from the data along the sampling dimension.

        Parameters
        ----------
        save: bool, optional
            Whether or not to save data in a new databin. (Default is True)

        Returns
        -------
        ndarray-like
            Detrended data
        """
        logger.info('Detrending data...')
        if not self._leading_time:
            raise ValueError('Can only perform detrending with a specified '
                             'leading sampling dimension')

        if save and not self._save_none:
            self.detrended = self._new_empty_databin(self.data.shape,
                                                     self.data.dtype,
                                                     self._DETRENDED)

        self.data = self._detrend_func(self.data, output_arr=self.detrended)
        self._set_curr_data_key(self._DETRENDED)
        return self.data

    def area_weight_data(self, save=True):
        """
        Perform a gridcell area weighting by latitude on the data. Required
        before EOF projection.

        TODO: Figure out what correct way to handle rotated pole grids
        (Maybe equivalent latitude field?)

        Parameters
        ----------
        save: bool, optional
            Whether or not to save data in a new databin. (Default is True)

        Returns
        -------
        ndarray-like
            Area-weighted data
        """
        logger.info('Area weighting data by latitude...')
        if self.LAT not in self._dim_idx.keys():
            logger.warning('No latitude dimension specified. No area weighting'
                           'was performed.')
            return self.data

        if save and not self._save_none:
            self.area_weighted = self._new_empty_databin(self.data.shape,
                                                         self.data.dtype,
                                                         self._AWGHT)

        # If pole is rotated the latitude weighting will be off, need latitude
        # on an equivalent unrotated grid to properly weight by area
        if self._rotated_pole:
            lats = self.get_coordinate_grids([self.EQUIVLAT])[self.EQUIVLAT]
        else:
            lats = self.get_coordinate_grids([self.LAT])[self.LAT]
        scale = np.sqrt(abs(np.cos(np.radians(lats))))

        if is_dask_array(self.data):
            awgt = self.data * scale
            da.store(awgt, self.area_weighted)
        else:
            awgt = self.data
            self.area_weighted[:] = ne.evaluate('awgt * scale')

        self.data = self.area_weighted
        self._set_curr_data_key(self._AWGHT)

    def eof_proj_data(self, num_eofs, save=True):
        """
        Calculate spatial EOFs on the data retaining a specified number of
        modes.

        Parameters
        ----------
        num_eofs: int
            How many modes to retain from the EOF decomposition.
        save: bool, optional
            Whether or not to save data in a new databin. (Default is True)

        Returns
        -------
        ndarray-like
            Data projected into EOF basis.  Will have shape of (sampling dim
            x num EOFs).
        """
        logger.info('Projecting data into leading {:d} EOFs'.format(num_eofs))
        if not self._leading_time:
            raise ValueError('Can only perform eof calculation with a '
                             'specified leading sampling dimension')

        if len(self.data.shape) > 2:
            logger.warning('Cannot perform EOF calculation on data with more '
                           'than 2 dimensions. Flattening data...')
            self._flatten_curr_data()

        if save and not self._save_none:
            new_shp = (self.data.shape[0], num_eofs)
            self.eof_proj = self._new_empty_databin(new_shp,
                                                    self.data.dtype,
                                                    self._EOFPROJ)

        self._eofs, self._svals = calc_eofs(self.data, num_eofs,
                                            var_stats_dict=self._eof_stats)

        if is_dask_array(self.data):
            proj = da.dot(self.data, self._eofs)
            da.store(proj, self.eof_proj)
        else:
            self.eof_proj[:] = np.dot(self.data, self._eofs)

        self.data = self.eof_proj
        self._set_curr_data_key(self._EOFPROJ)

        return self.data

    # TODO: Make this return copies of dim_coord information
    def get_dim_coords(self, keys):
        """
        Return dim_coord key, value pairs for a specified group of keys.

        Parameters
        ----------
        keys: Iterable<str>
            A list of keys specifying data to retrieve from the dim_coords
            property

        Returns
        -------
        dict
            A dim_coord dictionary with specified keys.  Values will be a tuple
            of the dimension index and coordinate values.
        """
        logger.info('Retrieving dim_coords for: {}'.format(keys))
        dim_coords = {}

        for key in keys:
            if key in self._dim_coords.keys():
                dim_coords[key] = self._dim_coords[key]

        return dim_coords

    def get_coordinate_grids(self, keys, compressed=True, flat=False):
        """
        Return coordinate grid for spatial dimensions in full, compressed, or
        flattened form.

        Parameters
        ----------
        keys: Iterable<str>
            A list of keys specifying spatial grids to create.
        compressed: bool, optional
            Whether or not to compress the grid when it contains masked values
        flat: bool, optional
            Whether or not to return a flattened 1D grid.

        Returns
        -------
        dict
            Requested coordinate grids as key/value pairs
        """
        logger.info('Retrieving coordinate grids for: {}'.format(keys))
        grids = {}

        if self.TIME in keys:
            logger.warning('Get_coordinate_grids currently only supports '
                           'retreival of spatial fields.')
            keys.pop(self.TIME)

        for key in keys:
            if key not in self._dim_idx.keys():
                raise KeyError('No matching dimension for key ({}) was found.'
                               ''.format(key))

            if key in self._coord_grids:
                grid = np.copy(self._coord_grids[key])
            else:
                idx = self._dim_idx[key]
                # adjust field index for leading time dimension
                if self._leading_time:
                    idx -= 1

                # Get coordinates for current key and copy
                coords = self._dim_coords[key][1]
                grid = np.copy(coords)

                # Expand dimensions for broadcasting
                for dim, _ in enumerate(self._spatial_shp):
                    if dim != idx:
                        grid = np.expand_dims(grid, dim)

                grid = np.ones(self._spatial_shp) * grid
                if self.is_masked and compressed:
                    grid = grid.flatten()
                    grid = grid[self.valid_data]
                elif flat:
                    grid = grid.flatten()

            grids[key] = grid

        return grids

    def reset_data(self, key):
        logger.info('Resetting data to: {}'.format(key))
        try:
            self.data = self._data_bins[key]
            if self._leading_time:
                # running mean alters the time TODO: check that this works
                self._time_shp = [self.data.shape[0]]
            self._set_curr_data_key(key)
        except KeyError:
            logger.error('Could not find {} in initialized '
                         'databins.'.format(key))
            raise KeyError('Key {} not saved.  Could not reset self.data.')

        return self.data

    def is_leading_time(self):
        return self._leading_time

    def save_dataobj_pckl(self, filename):

        logger.info('Saving data object to file: {}'.format(filename))

        tmp_dimcoord = self._dim_coords[self.TIME]
        tmp_time = tmp_dimcoord[1]
        topckl_time = ncf.date2num(tmp_time, units=self.time_units,
                                   calendar=self.time_cal)
        self._dim_coords[self.TIME] = (tmp_dimcoord[0], topckl_time)

        with open(filename, 'w') as f:
            cpk.dump(self, f)

        self._dim_coords[self.TIME] = (tmp_dimcoord[0], tmp_time)

    @classmethod
    def from_netcdf(cls, filename, var_name, **kwargs):

        logging.info('Loading data object from netcdf: \n'
                     'file = {}\n'
                     'var_name = {}'.format(filename, var_name))

        with ncf.Dataset(filename, 'r') as f:
            data = f.variables[var_name]
            lat = f.variables['lat']
            lon = f.variables['lon']

            if len(lat.shape) > 1:
                lat_grid = lat
                lon_grid = lon
                lat = lat[:, 0]
                lon = lon[0]
                grids = {BaseDataObject.LAT: lat_grid[:],
                         BaseDataObject.LON: lon_grid[:]}
            else:
                grids = None

            coords = {BaseDataObject.LAT: lat[:],
                      BaseDataObject.LON: lon[:]}
            times = f.variables['time']

            try:
                cal = times.calendar
                coords[BaseDataObject.TIME] = ncf.num2date(times[:], times.units,
                                                           calendar=cal)
            except AttributeError:
                logger.debug('No calendar attribute found in netCDF.')
                coords[BaseDataObject.TIME] = ncf.num2date(times[:], times.units)
                cal = None

            for i, key in enumerate(data.dimensions):
                if key in coords.keys():
                    coords[key] = (i, coords[key])

            force_flat = kwargs.pop('force_flat', True)
            return cls(data, dim_coords=coords, force_flat=force_flat,
                       time_units=times.units, time_cal=cal, coord_grids=grids,
                       **kwargs)

    @classmethod
    def from_hdf5(cls, filename, var_name, data_dir='/', **kwargs):

        logging.info('Loading data object from HDF5: \n'
                     'file = {}\n'
                     'var_name = {}'.format(filename, var_name))

        with tb.open_file(filename, 'r') as f:

            data = f.get_node(data_dir, name=var_name)
            try:
                fill_val = data.attrs.fill_value
            except AttributeError:
                fill_val = None

            lat = f.get_node(data_dir+'lat')
            lon = f.get_node(data_dir+'lon')
            lat_idx = lat.attrs.index
            lon_idx = lon.attrs.index

            if len(lat.shape) > 1:
                lat_grid = lat
                lon_grid = lon
                lat = lat[:, 0]
                lon = lon[0]
                grids = {BaseDataObject.LAT: lat_grid[:],
                         BaseDataObject.LON: lon_grid[:]}
            else:
                grids = None

            coords = {BaseDataObject.LAT: (lat_idx, lat[:]),
                      BaseDataObject.LON: (lon_idx, lon[:])}
            times = f.get_node(data_dir+'time')
            coords[BaseDataObject.TIME] = (times.attrs.index,
                                           ncf.num2date(times[:],
                                                        times.attrs.units))
            force_flat = kwargs.pop('force_flat', True)
            return cls(data, dim_coords=coords, force_flat=force_flat,
                       coord_grids=grids, fill_value=fill_val, **kwargs)

    @classmethod
    def from_pickle(cls, filename):
        logging.info('Loading data object from pickle.\n'
                     'file = {}'.format(filename))

        with open(filename, 'r') as f:
            dobj = cpk.load(f)

        tmp_dimcoord = dobj._dim_coords[dobj.TIME]
        tmp_time = tmp_dimcoord[1]
        topckl_time = ncf.num2date(tmp_time, units=dobj.time_units,
                                   calendar=dobj.time_cal)
        dobj._dim_coords[dobj.TIME] = (tmp_dimcoord[0], topckl_time)

        return dobj

    @classmethod
    def from_posterior_ncf(cls, filename, var_name, **kwargs):

        with ncf.Dataset(filename, 'r') as f:
            data = f.variables[var_name][:]
            coords = {BaseDataObject.LAT: f.variables['lat'][:],
                      BaseDataObject.LON: f.variables['lon'][:]}
            times = (0, f.variables['time'][:])

            coords['time'] = times
            coords['lat'] = (1, coords['lat'])
            coords['lon'] = (1, coords['lon'])

            return cls(data, dim_coords=coords, **kwargs)

    @classmethod
    def from_posterior_npc(cls, filename, **kwargs):
        with np.load(filename) as f:
            data = f['values'][:]
            lat = f['lat'][:, 0]
            lon = f['lon'][0, :]
            coords = {BaseDataObject.LAT: (1, lat),
                      BaseDataObject.LON: (1, lon),
                      BaseDataObject.TIME: (0, f['years'])}

            force_flat = kwargs.pop('force_flat', True)
            return cls(data, dim_coords=coords, force_flat=force_flat,
                       **kwargs)


class Hdf5DataObject(BaseDataObject):

    def __init__(self, data, h5file, dim_coords=None, valid_data=None,
                 force_flat=False, fill_value=None, chunk_shape=None,
                 default_grp='/data', rotated_pole=False, coord_grids=None):
        """
        Construction of a Hdf5DataObject from input data.  If nan or
        infinite values are present, a compressed version of the data
        is also stored.

        Parameters
        ----------
        data: ndarray
            Input dataset to be used.
        h5file: tables.File
            HDF5 Pytables file to use as a data storage backend
        dim_coords: dict(str:ndarray), optional
            Coordinate vector dictionary for supplied data.  Please use
            DataObject attributes (e.g. DataObject.TIME) for dictionary
            keys.
        valid_data: ndarray (np.bool), optional
            Array corresponding to valid data in the of the input dataset
            or uncompressed version of the input dataset.  Should have the same
            number of dimensions as the data and each  dimension should be
            greater than or equal to the spatial dimensions of data.
        force_flat: bool
            Force spatial dimensions to be flattened (1D array)
            Data has been detrended.
        fill_value: float
            Value to be considered invalid data during the mask and 
            compression. Only considered when data is not masked.
            
        default_grp: tables.Group or str, optional
            Group to store all created databins under in the hdf5 file.

        Notes
        -----
        If NaN values are present I do not suggest
        using the orig_data variable when reloading from a file.  Currently
        PyTables Carrays have no method of storing np.NaN so the values in those
        locations will be random.  Please only read the compressed data or make
        sure you apply the mask on the data if you think self.orig_data is being
        read from disk.
        """

        if type(h5file) != tb.File:
            logger.error('Invalid HDF5 file encountered: '
                         'type={}'.format(type(h5file)))
            raise ValueError('Input HDF5 file must be opened using pytables.')

        self.h5f = h5file
        self._default_grp = None
        self.set_databin_grp(default_grp)

        if chunk_shape is None:
            leading_time = BaseDataObject.TIME in dim_coords
            self._chunk_shape = self._determine_chunk(leading_time,
                                                      data.shape,
                                                      data.dtype)
        else:
            self._chunk_shape = chunk_shape

        logger.debug('Dask array chunk shape: {}'.format(self._chunk_shape))
        data = da.from_array(data, chunks=self._chunk_shape)

        super(Hdf5DataObject, self).__init__(data,
                                             dim_coords=dim_coords,
                                             valid_data=valid_data,
                                             force_flat=force_flat,
                                             fill_value=fill_value,
                                             rotated_pole=rotated_pole,
                                             coord_grids=coord_grids)

        self._eof_stats = None

    def _set_curr_data_key(self, key):
        if not hasattr(self.data, 'dask'):
            chunk_shp = self._determine_chunk(self._leading_time,
                                              self.data.shape,
                                              self.data.dtype)
            self._chunk_shape = chunk_shp
            logger.debug('Current chunk shape: {}'.format(chunk_shp))
            self.data = da.from_array(self.data, chunks=self._chunk_shape)
        super(Hdf5DataObject, self)._set_curr_data_key(key)

    # Create backend data container
    def _new_empty_databin(self, shape, dtype, name):
        logger.debug('Creating empty HDF5 databin:\n'
                     'shape: {}\n'
                     'dtype: {}\n'
                     'name: {}'.format(shape, dtype, name))
        new = empty_hdf5_carray(self.h5f,
                                self._default_grp,
                                name,
                                tb.Atom.from_dtype(dtype),
                                shape
                                )
        self._data_bins[name] = new
        return new

    def _new_databin(self, data, name):
        logger.debug('Copying data to HDF5 databin: {}'.format(name))
        new = self._new_empty_databin(data.shape, data.dtype, name)
        da.store(data, new)
        self._data_bins[name] = new
        return new

    @staticmethod
    def _determine_chunk(leading_time, shape, dtype, size=32):
        """
        Determine default chunk size for dask array operations.
        
        Parameters
        ----------
        shape: tuple<int>
            Shape of the data to be chunked.
        dtype: numpy.dtype
            Datatype of the data to be chunked
        size: int
            Size (in MB) of the desired chunk
        
        Returns
        -------
        tuple
            Chunk shape for data and given size.
        """
        if leading_time:
            sptl_size = np.product(shape[1:]) * dtype.itemsize
            rows_in_chunk = size*1024**2 // sptl_size
            rows_in_chunk = int(rows_in_chunk)
            chunk = tuple([rows_in_chunk] + list(shape[1:]))
        else:
            nelem = np.product(shape)
            elem_in_chunk = nelem*dtype.itemsize // (size * 1024**2)

            if elem_in_chunk == 0:
                chunk = shape
            else:
                dim_len = elem_in_chunk **(1./len(shape))
                dim_len = int(dim_len)
                chunk = tuple([dim_len for _ in shape])
        return chunk

    def _check_invalid_data(self, data):
        logger.info('Checking dask array data for invalid elements.')

        finite_data = da.isfinite(data)
        not_filled_data = data != self._fill_value
        valid_data = da.logical_and(finite_data, not_filled_data)

        if self._leading_time:
            time_len = data.shape[0]
            valid_data = valid_data.sum(axis=0) == time_len

        valid_data = valid_data.compute()
        masked = True

        if np.all(valid_data):
            valid_data = None
            masked = False

        return masked, valid_data

    def _compress_to_valid_data(self, data, valid, out_arr):
        logger.info('Compressing dask array data to valid spatial locations.')
        if self._leading_time:
            compress_axis = 1
        else:
            compress_axis = None

        compressed_data = da.compress(valid, data, axis=compress_axis)
        da.store(compressed_data, out_arr)
        return out_arr

    # TODO: Need to tie this to some more formal configuration
    @staticmethod
    def _detrend_func(data, output_arr=None, **kwargs):
        return dask_detrend_data(data, output_arr=output_arr)

    def calc_running_mean(self, window_size, year_len, save=True):

        if self._leading_time:
            orig = self._chunk_shape
            new_chunk = tuple([window_size*50] + list(orig[1:]))
            self.data = self.data.rechunk(new_chunk)
            logger.debug('New dask chunk shape for running mean: '
                         '{}'.format(new_chunk))

        res = super(Hdf5DataObject, self).calc_running_mean(window_size,
                                                            year_len,
                                                            save=save)

        if self._leading_time:
            res = res.rechunk(orig)
        
        return res

    def set_databin_grp(self, group):
        """
        Set the default PyTables group for databins to be created under in the
        HDF5 File.  This overwrites existing nodes with the same name and will
        create the full path necessary to reach the desired node.

        Parameters
        ----------
        group: tables.Group or str
            A PyTables group object or string path to set as the default group
            for the HDF5 backend to store databins.
        """
        if not type(group) == tb.Group and not type(group) == str:
            logger.error('Invalid group type encountered: '
                         '{}'.format(type(group)))
            raise ValueError('Input group must be of type PyTables.Group '
                             'or str.')

        # This is very hard to understand :/ so TODO: simplify
        try:
            self._default_grp = self.h5f.get_node(group)
            try:
                assert type(self._default_grp) == tb.Group
            except AssertionError:
                self.h5f.remove_node(self._default_grp)
                raise tb.NoSuchNodeError
        except tb.NoSuchNodeError:
            if type(group) == tb.Group:
                grp_path = path.split(group._v_pathname)
            else:
                grp_path = path.split(group)

            self._default_grp = self.h5f.create_group(grp_path[0], grp_path[1],
                                                      createparents=True)

    def save_dataobj_pckl(self, filename):
        self._tb_file_args = {'h5fname': self.h5f.filename,
                              'h5ffilt': self.h5f.filters,
                              'grp': self._default_grp._v_pathname}

        # temporary storage of hdf 5 file
        tmp_bins = {}
        h5f = self.h5f
        self.h5f = None

        # Set all HDF5 file connections to None
        for key in self._data_bins.keys():
            setattr(self, key, None)
            tmp_bins[key] = self._data_bins[key]
            self._data_bins[key] = None

        super(Hdf5DataObject, self).save_dataobj_pckl(filename)

        self.h5f = h5f
        for key, dbin in tmp_bins.iteritems():
            setattr(self, key, dbin)
            self._data_bins[key] = dbin

    @classmethod
    def from_netcdf(cls, filename, var_name, h5file, **kwargs):

        return super(Hdf5DataObject, cls).from_netcdf(filename, var_name,
                                                      h5file=h5file, **kwargs)

    @classmethod
    def from_hdf5(cls, filename, var_name, h5file, data_dir='/', **kwargs):

        return super(Hdf5DataObject, cls).from_hdf5(filename, var_name,
                                                    h5file=h5file,
                                                    data_dir=data_dir,
                                                    **kwargs)

    @classmethod
    def from_pickle(cls, filename):

        obj = super(Hdf5DataObject, cls).from_pickle(filename)

        tb_file_args = obj._tb_file_args
        h5fname = tb_file_args['h5fname']
        filters = tb_file_args['h5ffilt']
        group_path = tb_file_args['grp']

        h5f = tb.open_file(h5fname, mode='a', filters=filters)

        for key in obj._data_bins.keys():
            node = h5f.get_node(group_path, key)
            obj._data_bins[key] = node
            setattr(obj, key, node)

        obj.h5f = h5f
        obj._tb_file_args = None

        return obj


def var_to_hdf5_carray(h5file, group, node, data, **kwargs):
    """
    Take an input data and insert into a PyTables carray in an HDF5 file.

    Parameters
    ----------
    h5file: tables.File
        Writeable HDF5 file to insert the carray into.
    group: str, tables.Group
        PyTables group to insert the data node into
    node: str, tables.Node
        PyTables node of the carray.  If it already exists it will remove
        the existing node and create a new one.
    data: ndarray
        Data to be inserted into the node carray
    kwargs:
        Extra keyword arguments to be passed to the
        tables.File.create_carray method.

    Returns
    -------
    tables.carray
        Pointer to the created carray object.
    """
    assert(type(h5file) == tb.File)

    # Switch to string
    if type(group) != str:
        group = group._v_pathname

    # Join path for node existence check
    if group[-1] == '/':
        node_path = group + node
    else:
        node_path = '/'.join((group, node))

    # Check existence and remove if necessary
    if h5file.__contains__(node_path):
        h5file.remove_node(node_path)

    out_arr = h5file.create_carray(group,
                                   node,
                                   atom=tb.Atom.from_dtype(data.dtype),
                                   shape=data.shape,
                                   **kwargs)
    out_arr[:] = data
    return out_arr


def empty_hdf5_carray(h5file, group, node, in_atom, shape, **kwargs):
    """
    Create an empty PyTables carray.  Replaces node if it already exists.

    Parameters
    ----------
    h5file: tables.File
        Writeable HDF5 file to insert the carray into.
    group: str, tables.Group
        PyTables group to insert the data node into
    node: str, tables.Node
        PyTables node of the carray.  If it already exists it will remove
        the existing node and create a new one.
    in_atom: tables.Atom
        Atomic datatype and chunk size for the carray.
    shape: tuple, list
        Shape of empty carray to be created.
    kwargs:
        Extra keyword arguments to be passed to the
        tables.File.create_carray method.

    Returns
    -------
    tables.carray
        Pointer to the created carray object.
    """
    assert(type(h5file) == tb.File)

    # Switch to string
    if type(group) == tb.Group:
        group = group._v_pathname

    # Join path for node existence check
    if group[-1] == '/':
        node_path = group + node
    else:
        node_path = '/'.join((group, node))

    # Check existence and remove if necessary
    if h5file.__contains__(node_path):
        h5file.remove_node(node_path)

    out_arr = h5file.create_carray(group,
                                   node,
                                   atom=in_atom,
                                   shape=shape,
                                   **kwargs)
    return out_arr


def netcdf_to_hdf5_container(infile, var_name, outfile, data_dir='/'):
    """
    Transfer netCDF variable and latitude/longitude/time dimensions to an
    HDF5 container.
    
    Parameters
    ----------
    infile: str
        Path to netCDF file
    var_name: str
        Variable name to transfer from netCDF file.
    outfile: str
        Path for output HDF5 file. Uses PyTables storage format.
    data_dir: str, optional
        The directory in the HDF5 file to store the data at.  Defaults to the 
        root path ('/').
    """
    f = ncf.Dataset(infile, 'r')
    outf = tb.open_file(outfile, 'w', filters=tb.Filters(complib='blosc',
                                                         complevel=5))

    try:
        data = f.variables[var_name]
        atom = tb.Atom.from_dtype(data.datatype)
        shape = data.shape
        out = empty_hdf5_carray(outf, data_dir, var_name, atom, shape)

        spatial_nbytes = np.product(data.shape[1:])*data.dtype.itemsize
        tchunk_60mb = 60*1024**2 // spatial_nbytes
        fill_value = 1.0e20

        masked = False
        for k in xrange(0, shape[0], tchunk_60mb):
            if k == 0:
                data_chunk = data[k:k+tchunk_60mb]
                masked = np.ma.is_masked(data_chunk)
                if masked:
                    out.attrs.masked = True
                    out.attrs.fill_value = fill_value
            elif masked:
                data_chunk = data[k:k+tchunk_60mb].filled(1.0e20)
            else:
                data_chunk = data[k:k+tchunk_60mb]

            out[k:k+tchunk_60mb] = data_chunk

        lat = var_to_hdf5_carray(outf, data_dir, 'lat',
                                 f.variables['lat'][:])
        lon = var_to_hdf5_carray(outf, data_dir, 'lon',
                                 f.variables['lon'][:])
        times = f.variables['time']
        time_out = var_to_hdf5_carray(outf, data_dir, 'time',
                                      times[:])
        time_out.attrs.units = times.units

        coord_dims = {'lat': lat.attrs, 'lon': lon.attrs,
                      'time': time_out.attrs}

        for i, key in enumerate(f.dimensions.iterkeys()):
            if key in coord_dims.keys():
                coord_dims[key].index = i
    finally:
        f.close()
        outf.close()

