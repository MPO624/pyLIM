"""
Toolbox for statistical methods.

"""

import numpy as np
import tables as tb
from scipy.sparse.linalg import eigs

def runMean(data, window_size, h5_file=None, h5_parent=None):
    """
    A function for calculating the running mean on data.

    Parameters
    ----------
    data: ndarray
        Data matrix to perform running mean over. Expected to be in time(row) x
        space(column) format.
    window_size: int
        Size of the window to compute the running mean over.
    h5_file:  tables.file.File, optional
       Output hdf5 file (utilizes pyTables) for the calculated running mean.
    h5_parent: tables.group.*, optional
        Parent node to place run_mean dataset under.

    Returns
    -------
    result: ndarray
        Running mean result of given data.
    bot_edge: int
        Number of elements removed from beginning of the time series
    top_edge: int
        Number of elements removed from the ending of the time series
    """
    
    dshape = data.shape
    assert( dshape[0] >= window_size ), ("Window size must be smaller than or "
                                          "equal to the length of the time "
                                          "dimension of the data.")
    cut_from_top = window_size/2
    cut_from_bot = (window_size/2) + (window_size%2) - 1
    tot_cut = cut_from_top + cut_from_bot
    new_shape = list(dshape)
    new_shape[0] -= tot_cut

    if h5_file is not None:
        is_h5 = True
        if h5_parent is None:
            h5_parent = h5_file.root
        
        try:
            result = h5_file.create_carray(h5_parent, 
                                       'run_mean',
                                       atom = tb.Atom.from_dtype(data.dtype),
                                       shape = new_shape,
                                       title = '12-month running mean')
        except tb.NodeError:
            h5_file.remove_node(h5_parent.run_mean)
            result = h5_file.create_carray(h5_parent, 
                                       'run_mean',
                                       atom = tb.Atom.from_dtype(data.dtype),
                                       shape = new_shape,
                                       title = '12-month running mean')
            
    else:
        is_h5 = False                                       
        result = np.zeros(new_shape, dtype=data.dtype)
        
    for cntr in xrange(new_shape[0]):
        if cntr % 100 == 0:
            print 'Calc for index %i' % cntr
        result[cntr] = (data[(cntr):(cntr+tot_cut+1)].sum(axis=0) / 
                        float(window_size))
                        
    if is_h5:
        result = result.read()
    
    return (result, cut_from_bot, cut_from_top)
   

def calcEOF(data, num_eigs, retPCs = False):
    """
    Method to calculate the EOFs of given  dataset.  This assumes data comes in as
    an m x n matrix where m is the spatial dimension and n is the sampling
    dimension.  

    Parameters
    ----------
    data: ndarray
        Dataset to calculate EOFs from
    num_eigs: int
        Number of eigenvalues/vectors to return.  Must be less than min(m, n).
    retPCs: bool, optional
        Return principal component matrix along with EOFs

    Returns
    -------

    """
    
    eofs, E, pcs = np.linalg.svd(data, full_matrices=False)
    eig_vals = (E ** 2) / (len(E) - 1.)
    tot_var = (eig_vals[0:num_eigs].sum()) / eig_vals.sum()

    return (eofs[:,0:num_eigs], eig_vals[0:num_eigs], tot_var)

