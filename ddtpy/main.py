from __future__ import print_function, division

from copy import deepcopy
import json

import numpy as np
from numpy import fft
import math

from .psf import params_from_gs, gaussian_plus_moffat_psf_4d
from .model import DDTModel
from .data import read_dataset, read_select_header_keys, DDTData
from .adr import calc_paralactic_angle, differential_refraction
from .fitting import fit_model_all_epoch
from .registration import fit_position

__all__ = ["main"]


def main(filename):
    """Do everything.

    Parameters
    ----------
    filename : str
        JSON-formatted config file.
    """

    with open(filename) as f:
        conf = json.load(f)

    # check apodizer flag because the code doesn't support it
    if conf.get("FLAG_APODIZER", 0) >= 2:
        raise RuntimeError("FLAG_APODIZER >= 2 not implemented")

    spaxel_size = conf["PARAM_SPAXEL_SIZE"]
    
    # Reference wavelength. Used in PSF parameters and ADR.
    wave_ref = conf.get("PARAM_LAMBDA_REF", 5000.)

    # index of final ref. Subtract 1 due to Python zero-indexing.
    master_final_ref = conf["PARAM_FINAL_REF"] - 1

    # also want array with true/false for final refs.
    is_final_ref = np.array(conf.get("PARAM_IS_FINAL_REF"))

    n_iter_galaxy_prior = conf.get("N_ITER_GALAXY_PRIOR")

    # Load the header from the final ref or first cube
    i = master_final_ref if (master_final_ref >= 0) else 0
    header = read_select_header_keys(conf["IN_CUBE"][i])

    # Load data from list of FITS files.
    data, weight, wave = read_dataset(conf["IN_CUBE"])
    print(data[0,0,0] == 0)
    # Testing with only a couple wavelengths
    data = data[:, 200:201, :, :]*10**15
    weight = weight[:, 200:201, :, :]*10**(-15*2)
    wave = wave[200:201]
    
    
    # Zero-weight array elements that are NaN
    # TODO: why are there nans in here?
    mask = np.isnan(data)
    data[mask] = 0.0
    weight[mask] = 0.0

    ddtdata = DDTData(data, weight, wave, is_final_ref, master_final_ref,
                      header, spaxel_size)

    # Load PSF model parameters. Currently, the PSF in the model is
    # represented by an arbitrary 4-d array that is constructed
    # here. The PSF depends on some aspects of the data, such as
    # wavelength.  If different types of PSFs need to do different
    # things, we may wish to represent the PSF with a class, called
    # something like GaussMoffatPSF.
    #
    # GS-PSF --> ES-PSF
    # G-PSF --> GR-PSF
    if conf["PARAM_PSF_TYPE"] == "GS-PSF":
        es_psf_params = np.array(conf["PARAM_PSF_ES"])
        psf_ellipticity, psf_alpha = params_from_gs(
            es_psf_params, ddtdata.wave, wave_ref)
    elif conf["PARAM_PSF_TYPE"] == "G-PSF":
        raise RuntimeError("G-PSF (from FITS files) not implemented")
    else:
        raise RuntimeError("unrecognized PARAM_PSF_TYPE")

    # The following section relates to atmospheric differential
    # refraction (ADR): Because of ADR, the center of the PSF will be
    # different at each wavelength, by an amount that we can determine
    # (pretty well) from the atmospheric conditions and the pointing
    # and angle of the instrument. We calculate the offsets here as a function
    # of observation and wavelength and input these to the model.

    # atmospheric conditions at each observation time.
    airmass = np.array(conf["PARAM_AIRMASS"])
    p = np.asarray(conf.get("PARAM_P", 615.*np.ones_like(airmass)))
    t = np.asarray(conf.get("PARAM_T", 2.*np.ones_like(airmass)))
    h = np.asarray(conf.get("PARAM_H", np.zeros_like(airmass)))

    # Position of the instrument
    ha = np.deg2rad(np.array(conf["PARAM_HA"]))   # config files in degrees
    dec = np.deg2rad(np.array(conf["PARAM_DEC"])) # config files in degrees
    tilt = conf["PARAM_MLA_TILT"]

    # differential refraction as a function of time and wavelength,
    # in arcseconds (2-d array).
    delta_r = differential_refraction(airmass, p, t, h, ddtdata.wave, wave_ref)
    delta_r /= spaxel_size  # convert from arcsec to spaxels
    paralactic_angle = calc_paralactic_angle(airmass, ha, dec, tilt)
    adr_dx = -delta_r * np.sin(paralactic_angle)[:, None]  # O'xp <-> - east
    adr_dy = delta_r * np.cos(paralactic_angle)[:, None]

    # Make a first guess at the sky level based on the data.
    skyguess = ddtdata.guess_sky(2.0)

    # If target positions are given in the config file, set them in
    # the model.  (Otherwise, the positions default to zero in all
    # exposures.)
    if "PARAM_TARGET_XP" in conf:
        data_xctr_init = np.array(conf["PARAM_TARGET_XP"])
    else:
        data_xctr_init = np.zeros(ddtdata.nt)
    if "PARAM_TARGET_YP" in conf:
        data_yctr_init = np.array(conf["PARAM_TARGET_YP"])
    else:
        data_yctr_init = np.zeros(ddtdata.nt)

    # Initialize model
    model = DDTModel((ddtdata.nt, ddtdata.nw), psf_ellipticity, psf_alpha,
                     adr_dx, adr_dy, spaxel_size,
                     conf["MU_GALAXY_XY_PRIOR"],
                     conf["MU_GALAXY_LAMBDA_PRIOR"],
                     data_xctr_init, data_yctr_init,
                     skyguess)

    # Perform initial fit, holding position constant (at settings from
    # conf file PARAM_TARGET_[X,Y]P, directly above)
    # This fits the galaxy, SN and sky and updates the model accordingly,
    # keeping registration fixed.
    fit_model_all_epoch(model, ddtdata)

    # Fit registration on just the final refs
    # ==================================================================

    maxiter_fit_position = 100  # Max iterations in fit_position
    maxmove_fit_position = 3.0  # maxmimum movement allowed in fit_position
    mask_nmad = 2.5  # Minimum Number of Median Absolute Deviations above
                     # the minimum spaxel value in fit_position
    
    # Make a copy of the weight at this point, because we're going to
    # modify the weights in place for the next step.
    weight_orig = ddtdata.weight.copy()

    include_in_fit = np.ones(ddtdata.nt, dtype=np.bool)

    # /* Register the galaxy in the other final refs */
    # i_fit_galaxy_position=where(ddt.ddt_data.is_final_ref);
    # n_final_ref = numberof( i_fit_galaxy_position);
    # galaxy_offset = array(double, 2, ddt.ddt_data.n_t);
    
    # Loop over just the final refs excluding the master final ref.
    for i_t in range(ddtdata.nt):
        if (not ddtdata.is_final_ref[i_t]) or i_t == ddtdata.master_final_ref:
            continue
        
        xcoords = (np.arange(ddtdata.nx) - (ddtdata.nx - 1) / 2. +
                   model.data_xctr[i_t])
        ycoords = (np.arange(ddtdata.ny) - (ddtdata.ny - 1) / 2. +
                   model.data_yctr[i_t])
        m = model.evaluate(i_t, xcoords=xcoords, ycoords=ycoords, 
                           which='all')

        tmp_m = m.sum(axis=0)  # Sum of model over wavelengths (result = 2-d)
        tmp_mad = np.median(np.abs(tmp_m - np.median(tmp_m)))

        # Spaxels where model is greater than minimum + 2.5 * MAD
        mask = tmp_m > np.min(tmp_m) + mask_nmad * tmp_mad
        
        # If there is less than 20 spaxels available, we don't fit
        # the position
        if mask.sum() < 20:
            continue

        # This sets weight to zero on spaxels where mask is False
        # (where spaxel sum is less than minimum + 2.5 MAD)
        ddtdata.weight[i_t] = ddtdata.weight[i_t] * mask[None, :, :]

        # Fit the position.
        # TODO: should the sky be varied on each iteration?
        #       (currently, it is not varied)
        pos, convergence = fit_position(ddtdata, model, i_t,
                                        maxiter=maxiter_fit_position)
        
        # Check if the position moved too much from initial position.
        # If it didn't move too much, update the model.
        # If it did, cut it from the fitting for the next step.
        dist = math.sqrt((pos[0] - data_xctr_init[i_t])**2 + 
                         (pos[1] - data_yctr_init[i_t])**2)
        if dist < maxmove_fit_position:
            model.data_xctr[i_t] = pos[0]
            model.data_yctr[i_t] = pos[1]
        else:
            include_in_fit[i_t] = False

    # Reset weight
    ddtdata.weight = weight_orig

    # At L185 in run_ddt_galaxy_subtraction_all_regul_variable_all_epoch.i

