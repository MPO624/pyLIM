# My first blank OO code package to parse and bring into .ipynb exercises
### Brian Mapes, few years after last touches by originator

#### Clues: In LIM.py

class LIM(object):
    """Linear inverse forecast model.
    
    This class uses a calibration dataset to make simple linear forecasts. 
    Can perform forecasts using random or contiguous resampling, or with
    separate calibration and forecast datasets.
    
    Notes
    -----
    It's based on the LIM described by M. Newman (2013) [1].  Right now it
    assumes the use of monthly data (i.e. each timestep should represent a
    single month).
    
    References
    ----------
    .. [1] Newman, M. (2013), An Empirical Benchmark for Decadal Forecasts of 
       Global Surface Temperature Anomalies, J. Clim., 26(14), 5260–5269, 
       doi:10.1175/JCLI-D-12-00590.1.
       
    Examples
    --------
    ....
    """

    def __init__(self, calib_data_obj, wsize, fcast_times, fcast_num_pcs,
                 detrend_data=False, h5file=None, L_eig_bump=None):
        """
        Parameters
        ----------
        calib_data_object: DataTools.BaseDataObject or subclass
            Dataset for determining LIM forecast EOFs.  DataInput provids
            a 2D MxN matrix where M (rows) represent temporal samples and
            N(columns) represent spatial samples.  It handles data with
            nan masking.  Note: If data is masked, saved output spatial
            dimensions will be reduced to valid data.
        wsize: int
            Windowsize for running mean.  For this implementation it should
            be equal to a year's worth of samples
        fcast_times: array_like
            1D array-like object containing all times to forecast at with the
            LIM. Times should be in wsize units. i.e. 1yr forecast should
            be integer value "1" not 12 (if wsize=12).
        fcast_num_pcs: int
            Number of principal components to include in forecast calculations
        H5file: HDF5_Object, Optional
            File object to store LIM output.  It will create a series of
            directories under the given group
        """
'data_obj must be an instance of BaseDataObject'
