import os
import tables as tb
import numpy as np
import Stats as St
import DataTools as Dt
import matplotlib.pyplot as plt
import scipy.io.netcdf as ncf
from mpl_toolkits.basemap import Basemap
from matplotlib.colors import LinearSegmentedColormap
from itertools import izip

import matplotlib.cm as cm

__all__ = ['calc_anomaly']

#custom colormap information, trying to reproduce Newman
lb = tuple(np.array([150, 230, 255])/255.0)
w = (1.0, 1.0, 1.0)
yl = tuple(np.array([243, 237, 48])/255.0)
rd = tuple(np.array([255, 50, 0])/255.0)
dk = tuple(np.array([110, 0, 0])/255.0)

cdict = {'red':     ((0.0, lb[0], lb[0]),
                     (0.1, w[0], w[0]),
                     (0.3, yl[0], yl[0]),
                     (0.7, rd[0], rd[0]),
                     (1.0, dk[0], dk[0])),

         'green':   ((0.0, lb[1], lb[1]),
                     (0.2, w[1], w[1]),
                     (0.4, yl[1], yl[1]),
                     (0.7, rd[1], rd[1]),
                     (1.0, dk[1], dk[1])),

         'blue':    ((0.0, lb[2], lb[2]),
                     (0.2, w[2], w[2]),
                     (0.4, yl[2], yl[2]),
                     (0.7, rd[2], rd[2]),
                     (1.0, dk[2], dk[2]))}

newm = LinearSegmentedColormap('newman', cdict)


def fcast_corr(h5file):
    node_name = 'corr'
    parent = '/stats'

    assert(h5file is not None and type(h5file) == tb.File)

    try:
        obs = h5file.root.data.anomaly_srs[:]
        test_start_idxs = h5file.root.data.test_start_idxs[:]
        fcast_times = h5file.root.data.fcast_times[:]
        fcasts = h5file.list_nodes(h5file.root.data.fcast_bin)
        eofs = h5file.root.data.eofs[:]
        yrsize = h5file.root.data._v_attrs.yrsize
        test_tdim = h5file.root.data._v_attrs.test_tdim
    except tb.NodeError as e:
        raise type(e)(e.message + ' Returning without finishing operation...')
        return None

    atom = tb.Atom.from_dtype(obs.dtype)
    corr_shp = [len(fcast_times), obs.shape[1]]

    try:
        corr_out = Dt.empty_hdf5_carray(h5file, parent, node_name, atom,
                                        corr_shp,
                                        title="Spatial Correlation",
                                        createparents=True)
    except tb.FileModeError:
        corr_out = np.zeros(corr_shp)

    for i, lead in enumerate(fcast_times):
        print 'Calculating Correlation: %i yr fcast' % lead
        compiled_obs = build_trial_obs(obs, test_start_idxs, lead*yrsize, test_tdim)
        data = fcasts[i].read()
        phys_fcast = build_trial_fcast(data, eofs)

        # for j, trial in enumerate(data):
        #     phys_fcast = np.dot(trial.T, eofs[j].T)
        #     corr_out[i] += St.calc_ce(phys_fcast, compiled_obs[j], obs)

        corr_out[i] = St.calc_lac(phys_fcast, compiled_obs)

    return corr_out


def fcast_ce(h5file):
    node_name = 'ce'
    parent = '/stats'

    assert(h5file is not None and type(h5file) == tb.File)

    try:
        obs = h5file.root.data.anomaly_srs[:]
        test_start_idxs = h5file.root.data.test_start_idxs[:]
        fcast_times = h5file.root.data.fcast_times[:]
        fcasts = h5file.list_nodes(h5file.root.data.fcast_bin)
        eofs = h5file.root.data.eofs[:]
        yrsize = h5file.root.data._v_attrs.yrsize
        test_tdim = h5file.root.data._v_attrs.test_tdim
    except tb.NodeError as e:
        raise type(e)(e.message + ' Returning without finishing operation...')
        return None

    atom = tb.Atom.from_dtype(obs.dtype)
    ce_shp = [len(fcast_times), obs.shape[1]]

    try:
        ce_out = Dt.empty_hdf5_carray(h5file, parent, node_name, atom, ce_shp,
                                      title="Spatial Coefficient of Efficiency",
                                      createparents=True)
    except tb.FileModeError:
        ce_out = np.zeros(ce_shp)

    for i, lead in enumerate(fcast_times):
        print 'Calculating CE: %i yr fcast' % lead
        compiled_obs = build_trial_obs(obs, test_start_idxs, lead*yrsize, test_tdim)
        data = fcasts[i].read()
        for j, trial in enumerate(data):
            phys_fcast = np.dot(trial.T, eofs[j].T)
            ce_out[i] += St.calc_ce(phys_fcast, compiled_obs[j], obs)

        ce_out[i] /= float(len(data))
    
    return ce_out


def calc_anomaly(data, yrsize, climo=None):
    old_shp = data.shape
    new_shp = (old_shp[0]/yrsize, yrsize, old_shp[1])
    if climo is None:
        climo = data.reshape(new_shp).sum(axis=0)/float(new_shp[0])
    anomaly = data.reshape(new_shp) - climo
    return anomaly.reshape(old_shp), climo


def build_trial_obs(obs, start_idxs, tau, test_dim):

    dat_shp = [len(start_idxs)*test_dim, obs.shape[-1]]
    obs_data = np.zeros(dat_shp, dtype=obs.dtype)

    for i, idx in enumerate(start_idxs):
        i0 = i*test_dim
        ie = i*test_dim + test_dim

        obs_data[i0:ie] = obs[(idx+tau):(idx+tau+test_dim)]
        
    return obs_data


def build_trial_fcast(fcast_trials, eofs):

    t_shp = fcast_trials.shape
    dat_shp = [t_shp[0]*t_shp[-1], eofs.shape[1]]
    phys_fcast = np.zeros(dat_shp, dtype=fcast_trials.dtype)

    for i, (trial, eof) in enumerate(izip(fcast_trials, eofs)):
        i0 = i*t_shp[-1]  # i * (test time dimension)
        ie = i*t_shp[-1] + t_shp[-1]

        phys_fcast[i0:ie] = np.dot(trial.T, eof.T)

    return phys_fcast


def area_wgt(data, lats):
    assert(data.shape[-1] == lats.shape[-1])
    scale = np.sqrt(np.cos(np.radians(lats)))
    return data * scale


def load_landsea_mask(maskfile, tile_len):
    f_mask = ncf.netcdf_file(maskfile)
    land_mask = f_mask.variables['land']
    
    try:
        sf = land_mask.scale_factor
        offset = land_mask.add_offset
        land_mask = land_mask.data*sf + offset
    except AttributeError:
        land_mask = land_mask.data
        
    land_mask = land_mask.squeeze().astype(np.int16).flatten()
    sea_mask = np.logical_not(land_mask)
    
    tiled_landmask = np.repeat(np.expand_dims(land_mask, 0),
                               tile_len,
                               axis=0)
    tiled_seamask = np.repeat(np.expand_dims(sea_mask, 0),
                              tile_len,
                              axis=0)
    return tiled_landmask, tiled_seamask
    
####  PLOTTING FUNCTIONS  ####


def plot_corrdata(lats, lons, data, title, outfile=None):
    plt.clf()
    contourlev = np.concatenate(([-1], np.linspace(0, 1, 11)))
    cbticks = np.linspace(0, 1, 11)
    plt.close('all')
    m = Basemap(projection='gall', llcrnrlat=-90, urcrnrlat=90,
                llcrnrlon=0, urcrnrlon=360, resolution='c')
    m.drawcoastlines()
    color = newm
    color.set_under('#9acce5')
    m.contourf(lons, lats, data, latlon=True, cmap=color,
               vmin=0, levels=contourlev)
    m.colorbar(ticks=cbticks)
    plt.title(title)
    
    if outfile is not None:
        plt.savefig(outfile, format='png')
    else:
        plt.show()


def plot_cedata(lats, lons, data, title, outfile=None):
    #contourlev = np.concatenate(([-1],np.linspace(0,1,11)))
    #cbticks = np.linspace(0,1,11)
    plt.close('all')
    m = Basemap(projection='gall', llcrnrlat=-90, urcrnrlat=90,
                llcrnrlon=0, urcrnrlon=360, resolution='c')
    m.drawcoastlines()
    
    # contourlev = np.linspace(0, 1, 11)
    
    if data.min() < 0:
        color = cm.bwr
        # neglev = np.linspace(-1, 0, 11)
        # contourlev = np.concatenate((neglev, contourlev))
    else:
        color = cm.OrRd
        
    m.pcolor(lons, lats, data, latlon=True, cmap=color, vmin=-1, vmax=1)
    m.colorbar()
    plt.title(title)
    if outfile is not None:
        plt.savefig(outfile, format='png')
    else:
        plt.show()


def plot_spatial(lats, lons, data, title, outfile=None):
    """
    Method for basic spatial data plots.  Uses diverging color scheme, so 
    current implementation is best for anomaly data.  Created initially just
    to plot spatial EOFs
    
    Parameters
    ----------
    lats: ndarray
        MxN matrix of latitude values
    lons: ndarray
        MxN matrix of longitude values
    data: ndarray
        MxN matrix of spatial data to plot
    title: str
        Title string for the plot
    outfile: str
        Filename to save the png image as
    only_pos: bool
        Changes colormap if you are using only positive values.
    """
    plt.clf()
    plt_range = np.max(np.abs(data))
    m = Basemap(projection='gall', llcrnrlat=-90, urcrnrlat=90,
                llcrnrlon=0, urcrnrlon=360, resolution='c')
    m.drawcoastlines()

    if data.min() >= 0:
        color = cm.OrRd
    else:
        color = cm.bwr

    m.pcolor(lons, lats, data, latlon=True, cmap=color, vmin=-plt_range,
             vmax=plt_range)
    m.colorbar()
    
    plt.title(title)

    if outfile is not None:
        plt.savefig(outfile, format='png')
    else:
        plt.show()


def plot_vstau(fcast_data, eof_data, obs, obs_tidxs, loc, title, outfile):
    fcast_tlim = fcast_data.shape[1]
    evar = np.zeros(fcast_tlim)
    for tau in range(fcast_tlim):
        tmpdata = fcast_data[:, tau]
        reconstructed = np.array([
                                 np.dot(eof_data[loc], fcast)
                                 for fcast in tmpdata
                                 ])
        truth = np.array([obs.T[loc, idxs] for idxs in obs_tidxs])
        error = reconstructed - truth
        evar[tau] = error.var()
        
    fig, ax = plt.subplots()
    ax.plot(evar)
    ax.set_title(title)
    ax.set_xlabel('Lead time (months)')
    ax.set_ylabel('Error Variance (K)')
    fig.savefig(outfile, format='png')


def plot_vstime(obs, loc):
    #Variance and mean vs time sample in true space
    var_vs_time = np.array([obs.T[loc, 0:i].var() 
                            for i in range(1, obs.shape[0])])
    mean_vs_time = np.array([obs.T[loc, 0:i].mean()
                            for i in range(1, obs.shape[0])])
    varfig, varax = plt.subplots()
    varax.plot(var_vs_time, label='Variance')
    varax.plot(mean_vs_time, label='Mean')
    varax.axvline(x=0, color='r')
    #varax.axvline(x = time_dim, color = 'r')
    #varax.axvline(x = forecast_tlim, color = 'y')
    #varax.axvline(x = shp_anomaly.shape[0], color = 'y')
    varax.axhline(y=0, linewidth=1, c='k')
    varax.set_title('variance and mean w/ increasing time sample')
    varax.set_xlabel('Times included (0 to this month)')
    varax.set_ylabel('Variance & Mean (K)')
    varax.legend(loc=9)
    varfig.show()
    
    runfig, runax = plt.subplots()
    runax.plot(obs.T[loc, :])
    runax.set_title('Time series at loc = %i (12-mon running mean)' % loc)
    runax.set_xlabel('Month')
    runax.set_ylabel('Temp Anomaly (K)')
    runfig.show()


def plot_vstrials(fcast_data, obs, test_tidxs, test_tdim, tau, loc):
    num_trials = fcast_data.shape[0]/test_tdim
    anom_truth = build_trial_obs(obs, test_tidxs, tau, test_tdim)
    loc_tru_var = anom_truth[:, loc].var()
    print loc_tru_var
    loc_tru_mean = anom_truth[:, loc].mean()
    
    fcast_var = np.zeros(num_trials)
    fcast_mean = np.zeros(num_trials)
    
    for i in xrange(num_trials):
        end = i*test_tdim + test_tdim
        fcast_var[i] = fcast_data[0:end, loc].var()
        fcast_mean[i] = fcast_data[0:end, loc].mean()
    
    fig, ax = plt.subplots(2, 1, sharex=True)
    
    ax[0].plot(fcast_var, color='b', linewidth=2, label='Fcast Var')
    ax[0].axhline(loc_tru_var, xmin=0, xmax=num_trials,
                  linestyle='--', color='k', label='True Var')
    ax[0].legend(loc=4)
    ax[1].plot(fcast_mean, linewidth=2, label='Fcast Mean')
    ax[1].axhline(loc_tru_mean, xmin=0, xmax=num_trials,
                  linestyle='--', color='k', label='True Mean')
    ax[1].legend()
    
    # Interesting case of line matching below here
    #for line, var in zip(ax[0].get_lines(), true_var):
    #    ax[0].axhline(y=var, linestyle = '--', color = line.get_color())
    
    ax[0].set_title('Forecast Variance & Mean vs. # Trials (Single Gridpoint)'
                    ' Tau = %i' % tau)
    ax[0].set_ylim(0, 0.8)
    ax[1].set_xlabel('Trial #')
    ax[0].set_ylabel('Variance (K)')
    ax[1].set_ylabel('Mean (K)')
    fig.show()
    

if __name__ == "__main__":
    if os.name == 'nt':
        outf = 'G:\Hakim Research\pyLIM\LIM_data.h5'
        #outf = 'G:\Hakim Research\pyLIM\Trend_LIM_data.h5'
    else:
        #outf = '/home/chaos2/wperkins/data/pyLIM/LIM_data.h5'
        #outf = '/home/chaos2/wperkins/data/pyLIM/Detrend_LIM_data.h5'
        outf = '/home/chaos2/wperkins/data/pyLIM/Trended_sepEOFs_LIM_data.h5'
    h5f = tb.open_file(outf, mode='a')
    try:
        corr = fcast_corr(h5f)
        ce = fcast_ce(h5f)
    finally:
        h5f.close()