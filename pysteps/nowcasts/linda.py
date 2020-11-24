"""
pysteps.nowcasts.linda
======================

This module implements the Lagrangian INtegro-Difference equation model with
Autoregression (LINDA). The model combines features from extrapolation,
S-PROG, STEPS and ANVIL, integro-difference equation (IDE) and cell tracking
methods with the aim of producing improved nowcasts for convective rainfall. It
consists of the following components:

1. feature detection to identify rain cells
2. advection-based nowcast
3. autoregressive integrated (ARI) process to predict growth and decay
4. convolution to account for loss of predictability
5. stochastic perturbations to simulate forecast errors

To maximize computational performance and allow localizatin, LINDA uses a sparse
feature-based representation of the input data. Building on extrapolation nowcast,
the remaining temporal evolution of the rainfall fields is modeled in the
Lagrangian coordinates. Using the ARI process is adapted from ANVIL
:cite:`PCLH2020`, and the convolution is adapted from the integro-difference
equation (IDE) methodology proposed in :cite:`FW2005` and :cite:`XWF2005`.
Combination of these two approaches essentially replaces the cascade
decomposition and the AR process used in S-PROG and STEPS. The convolution gives
several advantages such as the ability to handle anisotropic structure, domain
boundaries and missing data. Based on the marginal distribution and covariance
structure of forecast errors, localized perturbations are generated by adapting
the SSFT methodology developed in :cite:`NBSG2017`.

.. autosummary::
    :toctree: ../generated/

    forecast
"""

try:
    import dask

    DASK_IMPORTED = True
except ImportError:
    DASK_IMPORTED = False
import numpy as np
from scipy.optimize import least_squares, LinearConstraint, minimize, minimize_scalar
from scipy.signal import convolve
from pysteps import extrapolation
from pysteps import utils


def forecast(
    precip_fields,
    advection_field,
    num_timesteps,
    ari_order=1,
    add_perturbations=True,
    num_ens_members=24,
    feature_method="blob",
    feature_kwargs={},
    kernel_type="anisotropic",
    interp_window_radius=25,
    extrap_method="semilagrangian",
    extrap_kwargs={},
    num_workers=1,
):
    """Generate a nowcast ensemble by using the Lagrangian INtegro-Difference
    equation model with Autoregression (LINDA).

    Parameters
    ----------
    precip_fields : array_like
        Array of shape (ari_order + 2, m, n) containing the input precipitation
        fields ordered by timestamp from oldest to newest. The time steps
        between the inputs are assumed to be regular.
    advection_field : array_like
        Array of shape (2, m, n) containing the x- and y-components of the
        advection field. The velocities are assumed to represent one time step
        between the inputs.
    num_timesteps : int
        Number of time steps to forecast.
    ari_order : {1, 2}
        The order of the ARI(p,1) model.
    add_perturbations : bool
        Set to False to disable perturbations and generate a deterministic
        nowcast.
    num_ens_members : int
        Number of ensemble members.
    feature_method : {'blob', 'domain' 'grid', 'shitomasi'}
        Feature detection method:

        +-------------------+-----------------------------------------------------+
        |    Method name    |                  Description                        |
        +===================+=====================================================+
        |  blob             | Laplace of Gaussian (LoG) blob detector implemented |
        |                   | in scikit-image                                     |
        +-------------------+-----------------------------------------------------+
        |  domain           | no feature detection, the model is applied over the |
        |                   | whole domain without localization                   |
        +-------------------+-----------------------------------------------------+
        |  grid             | no feature detection: the coordinates of the        |
        |                   | localization windows are aligned in a grid          |
        +-------------------+-----------------------------------------------------+
        |  shitomasi        | Shi-Tomasi corner detector implemented in OpenCV    |
        +-------------------+-----------------------------------------------------+
    feature_kwargs : dict, optional
        Keyword arguments that are passed as **kwargs for the feature detector.
    kernel_type : {"anisotropic", "isotropic"}
        The type of the kernel. Default : 'anisotropic'.
    interp_window_radius : int
        The standard deviation of the Gaussian kernel for computing the
        interpolation weights. Default : 25.
    extrap_method : str, optional
        The extrapolation method to use. See the documentation of
        pysteps.extrapolation.interface.
    extrap_kwargs : dict, optional
        Optional dictionary containing keyword arguments for the extrapolation
        method. See the documentation of pysteps.extrapolation.
    num_workers : int
        The number of workers to use for parallel computations. Applicable if
        dask is installed. When num_workers>1, it is advisable to disable
        OpenMP by setting the environment variable OMP_NUM_THREADS to 1. This
        avoids slowdown caused by too many simultaneous threads.

    Returns
    -------
    out : numpy.ndarray
        A four-dimensional array of shape (num_ens_members, num_timesteps, m, n)
        containing a time series of forecast precipitation fields for each
        ensemble member. The time series starts from t0 + timestep, where
        timestep is taken from the input fields.
    """
    localized_nowcasts = np.empty(
        (num_features, precip_fields.shape[1], precip_fields.shape[2])
    )
    window_weights = _compute_window_weights(
        feature_coords, precip_fields.shape[1], precip_fields.shape[2], window_radii
    )

    # iterate each time step
    for t in range(num_timesteps):
        np.sum(window_weights * localized_nowcasts, axis=0)


# Compute anisotropic Gaussian convolution kernel
def _compute_kernel_anisotropic(params, cutoff=6.0):
    phi, sigma1, sigma2 = params[:3]

    sigma1 = abs(sigma1)
    sigma2 = abs(sigma2)

    phi_r = phi / 180.0 * np.pi
    R_inv = np.array([[np.cos(phi_r), np.sin(phi_r)], [-np.sin(phi_r), np.cos(phi_r)]])

    bb_y1, bb_x1, bb_y2, bb_x2 = _compute_ellipse_bbox(phi, sigma1, sigma2, cutoff)

    x = np.arange(int(bb_x1), int(bb_x2) + 1).astype(float)
    if len(x) % 2 == 0:
        x = np.arange(int(bb_x1) - 1, int(bb_x2) + 1).astype(float)
    y = np.arange(int(bb_y1), int(bb_y2) + 1).astype(float)
    if len(y) % 2 == 0:
        y = np.arange(int(bb_y1) - 1, int(bb_y2) + 1).astype(float)

    X, Y = np.meshgrid(x, y)
    XY = np.vstack([X.flatten(), Y.flatten()])
    XY = np.dot(R_inv, XY)

    x2 = XY[0, :] * XY[0, :]
    y2 = XY[1, :] * XY[1, :]
    result = np.exp(-((x2 / sigma1 + y2 / sigma2) ** params[3]))
    result /= np.sum(result)

    return np.reshape(result, X.shape)


def _compute_kernel_isotropic(params, cutoff=6.0):
    sigma = params[0]

    bb_y1, bb_x1, bb_y2, bb_x2 = (
        -sigma * cutoff,
        -sigma * cutoff,
        sigma * cutoff,
        sigma * cutoff,
    )

    x = np.arange(int(bb_x1), int(bb_x2) + 1).astype(float)
    if len(x) % 2 == 0:
        x = np.arange(int(bb_x1) - 1, int(bb_x2) + 1).astype(float)
    y = np.arange(int(bb_y1), int(bb_y2) + 1).astype(float)
    if len(y) % 2 == 0:
        y = np.arange(int(bb_y1) - 1, int(bb_y2) + 1).astype(float)

    X, Y = np.meshgrid(x / sigma, y / sigma)

    r2 = X * X + Y * Y
    result = np.exp(-0.5 * r2)

    return result / np.sum(result)


# Compute the bounding box of an ellipse
def _compute_ellipse_bbox(phi, sigma1, sigma2, cutoff):
    r1 = cutoff * sigma1
    r2 = cutoff * sigma2
    phi_r = phi / 180.0 * np.pi

    if np.abs(phi_r - np.pi / 2) > 1e-6 and np.abs(phi_r - 3 * np.pi / 2) > 1e-6:
        alpha = np.arctan(-r2 * np.sin(phi_r) / (r1 * np.cos(phi_r)))
        w = r1 * np.cos(alpha) * np.cos(phi_r) - r2 * np.sin(alpha) * np.sin(phi_r)

        alpha = np.arctan(r2 * np.cos(phi_r) / (r1 * np.sin(phi_r)))
        h = r1 * np.cos(alpha) * np.sin(phi_r) + r2 * np.sin(alpha) * np.cos(phi_r)
    else:
        w = sigma2 * cutoff
        h = sigma1 * cutoff

    return -abs(h), -abs(w), abs(h), abs(w)


def _compute_window_weights(coords, grid_height, grid_width, window_radii):
    coords = coords.astype(float).copy()
    num_features = coords.shape[0]

    coords[:, 0] /= grid_height
    coords[:, 1] /= grid_width

    window_radii_1 = window_radii / grid_height
    window_radii_2 = window_radii / grid_width

    grid_x = (np.arange(grid_width) + 0.5) / grid_width
    grid_y = (np.arange(grid_height) + 0.5) / grid_height

    grid_x, grid_y = np.meshgrid(grid_x, grid_y)

    w = np.empty((num_features, grid_x.shape[0], grid_x.shape[1]))

    if coords.shape[0] > 1:
        for i, c in enumerate(coords):
            dy = c[0] - grid_y
            dx = c[1] - grid_x

            w[i, :] = np.exp(
                -dy * dy / (2 * window_radii_1[i] ** 2)
                - dx * dx / (2 * window_radii_2[i] ** 2)
            )
    else:
        w[0, :] = np.ones((grid_height, grid_width))

    return w


# Get anisotropic convolution kernel parameters from the given parameter vector.
def _get_anisotropic_kernel_params(p):
    theta = np.arctan2(p[1], p[0])
    sigma1 = np.sqrt(p[0] * p[0] + p[1] * p[1])
    sigma2 = sigma1 * p[2]

    return theta, sigma1, sigma2, p[3]


# TODO: use the method implemented in pysteps.timeseries.autoregression
def _iterate_ar_model(input_fields, psi):
    input_field_new = 0.0

    for i in range(len(psi)):
        input_field_new += psi[i] * input_fields[-(i + 1), :]

    return np.concatenate([input_fields[1:, :], input_field_new[np.newaxis, :]])


# Compute a 2d convolution by ignoring non-finite values.
def _masked_convolution(field, kernel):
    mask = np.isfinite(field)

    field = field.copy()
    field[~mask] = 0.0

    field_c = np.ones(field.shape) * np.nan
    field_c[mask] = convolve(field, kernel, mode="same")[mask]
    field_c[mask] /= convolve(mask.astype(float), kernel, mode="same")[mask]

    return field_c


# Constrained optimization of AR(1) parameters
def _optimize_ar1_params(field_src, field_dst, weights):
    def objf(p, *args):
        field_ar = p * field_src[0]
        return np.nansum(weights * (field_dst - field_ar) ** 2.0)

    bounds = (-0.98, 0.98)
    p_opt = minimize_scalar(objf, method="bounded", bounds=bounds)

    return p_opt.x


# Constrained optimization of AR(2) parameters
def _optimize_ar2_params(field_src, field_dst, weights):
    def objf(p, *args):
        field_ar = p[0] * field_src[1] + p[1] * field_src[0]
        return np.nansum(weights * (field_dst - field_ar) ** 2.0)

    bounds = [(-1.98, 1.98), (-0.98, 0.98)]
    constraints = [
        LinearConstraint(
            np.array([(1, 1), (-1, 1)]),
            (-np.inf, -np.inf),
            (0.98, 0.98),
            keep_feasible=True,
        )
    ]
    p_opt = minimize(
        objf,
        (0.8, 0.0),
        method="trust-constr",
        bounds=bounds,
        constraints=constraints,
    )

    return p_opt.x


def _optimize_convol_params(
    field_src,
    field_dst,
    weights,
    mask,
    kernel_type="anisotropic",
    kernel_params={},
    method="trf",
    num_workers=1,
):
    mask = np.logical_and(mask, weights > 1e-3)

    def objf(p, *args):
        if kernel_type == "anisotropic":
            p = _get_anisotropic_kernel_params(p)
            kernel = _compute_kernel_anisotropic(p, **kernel_params)
        else:
            kernel = _compute_kernel_isotropic((p,), **kernel_params)

        field_src_c_ = _masked_convolution(field_src, kernel)

        if kernel_type == "anisotropic" and method == "trf":
            fval = np.sqrt(weights[mask]) * (field_dst[mask] - field_src_c_[mask])
        else:
            fval = np.sum(weights[mask] * (field_dst[mask] - field_src_c_[mask]) ** 2)

        return fval

    if kernel_type == "anisotropic":
        if method == "lbfgsb":
            bounds = np.array([(-10.0, 10.0), (-10.0, 10.0), (0.25, 4.0), (0.1, 5.0)])
            p_opt = minimize(
                objf,
                np.array((1.0, 1.0, 1.0, 1.0)),
                bounds=bounds,
                method="L-BFGS-B",
            )
        elif method == "trf":
            bounds = np.array([(-10.0, -10.0, 0.25, 0.1), (10.0, 10.0, 4.0, 5.0)])
            p_opt = least_squares(
                objf,
                np.array((1.0, 1.0, 1.0, 1.0)),
                bounds=bounds,
                method="trf",
                ftol=1e-6,
                xtol=1e-6,
                gtol=1e-6,
            )
        p_opt = _get_anisotropic_kernel_params(p_opt.x)
    else:
        p_opt = minimize_scalar(objf, bounds=[0.01, 10.0], method="bounded")
        p_opt = p_opt.x

    return _compute_kernel_anisotropic(p_opt, **kernel_params)
