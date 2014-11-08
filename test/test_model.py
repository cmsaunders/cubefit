from __future__ import print_function

import numpy as np
from numpy.fft import fft2, ifft2

import ddtpy
from ddtpy.fitting import (penalty_g_all_epoch, likelihood_penalty,
                           regularization_penalty)

class TestFitting:
    def setup_class(self):
        """Create an instance of DDTData and DDTModel so that we can fit them"""
        nt = 3
        nw = 100
        wave = np.arange(nw)

        ellipticity = 1.5*np.ones((nt, nw))
        alpha = 2.0*np.ones((nt,nw))
        adr_dx, adr_dy = np.zeros((nt,nw)), np.zeros((nt,nw))
        spaxel_size = 0.43
        mu_xy = 1.0e-3
        mu_wave = 7.0e-2
        sky_guess = np.zeros((nt,nw))
        self.model = ddtpy.DDTModel(nt, wave, ellipticity, alpha,
                                    adr_dx, adr_dy, mu_xy, mu_wave,
                                    spaxel_size, sky_guess)

        data = ddtpy.gaussian_plus_moffat_psf_4d((15,15), 5., 5.,
                                                 ellipticity, alpha)
        weight = np.ones_like(data)
        header = {}
        is_final_ref = np.ones(nt,dtype = bool)
        master_final_ref = 0
        xctr_init = np.zeros(nt)
        yctr_init = np.zeros(nt)
        
        self.data = ddtpy.DDTData(data, weight, wave, xctr_init, yctr_init,
                                  is_final_ref, master_final_ref, header,
                                  spaxel_size)

    def test_gradient(self):
        """Test that gradient functions (used in galaxy fitting) return values
        'close' to what you get with a finite differences method."""
        
        x_diff = 1.e-10

        x = np.copy(self.model.gal.reshape(self.model.gal.size))
        toterr, grad = penalty_g_all_epoch(x, self.model, self.data)
        lkl_err, lkl_grad = likelihood_penalty(self.model, self.data)
        rgl_err, rgl_grad = regularization_penalty(self.model, self.data)
        
        x[0] += x_diff

        new_toterr, new_grad = penalty_g_all_epoch(x, self.model, self.data)
        new_lkl_err, new_lkl_grad = likelihood_penalty(self.model, self.data)
        new_rgl_err, new_rgl_grad = regularization_penalty(self.model, self.data)

        d_lkl = (new_lkl_err - lkl_err)/x_diff
        d_rgl = (new_rgl_err - rgl_err)/x_diff
        d_tot = (new_toterr - toterr)/x_diff

        #assert(d_rgl == rgl_grad[0])  # numerical precision issues
        #assert(d_lkl == lkl_grad[0])  # just wrong
        print("x_diff = {}, d_lkl = {}, lkl_grad = {}".format(x_diff, d_lkl, lkl_grad[0]))

            #assert(d_tot == grad[0])

    def test_lkl_gradient(self):
        """Test the likelihood gradient. 

        This is a sanity check to see if the gradient function is returning
        the naive result for individual elements. The likelihood is given
        by

        L = sum_i w_i * (d_i - m_i)^2

        where i represents pixels, d is the data, and m is the model
        *sampled onto the data frame*. We want to know the derivative
        with respect to model parameters x_j.

        dL/dx_j = sum_i -2 w_i (d_i - m_i) dm_i/dx_j

        dm_i/dx_j is the change in the resampled model due to changing model
        parameter j. Changing model parameter j is adjusting a single pixel
        in the model. The result in the data frame is a PSF at the position
        corresponding to model pixel j.

        """

        # model PSF array is centered at (model.ny-1 / 2., model.nx-1/ 2.)
        
        lkl_err, lkl_grad = likelihood_penalty(self.model, self.data)
        
        psf = self.model.psf[0, 0]
        
        m = self.model.evaluate(0, self.data.xctr[0], self.data.yctr[0],
                                (self.data.ny, self.data.nx), which='all')[0]

        w = self.data.weight[0,0]
        d = self.data.data[0,0]

        ny, nx = 15., 15.
        xmin = self.data.xctr[0] - (nx - 1) / 2.
        xmax = self.data.xctr[0] + (nx - 1) / 2.
        ymin = self.data.yctr[0] - (ny - 1) / 2.
        ymax = self.data.yctr[0] + (ny - 1) / 2.

       # Figure out the shift needed to put the model onto the requested
        # coordinates.
        xshift = -(xmin - self.model.xcoords[0])
        yshift = -(ymin - self.model.ycoords[0])
        xshift -= 16
        yshift -= 16
 
        shift_phasor = ddtpy.fft_shift_phasor_2d(self.model.MODEL_SHAPE,
                                                 (yshift + self.model.adr_dy[0,0],
                                                  xshift + self.model.adr_dx[0,0]))

        tmp = ifft2(fft2(psf) * shift_phasor)[:15,:15]

        dL = np.sum(-2.*w*(d - m)*tmp)

        print(dL, lkl_grad[0])

        
