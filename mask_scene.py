#!/usr/bin/env python2
# -*- coding: UTF-8 -*-

# Version 3.0; Ryan Shellberg, Erik Husby; Polar Geospatial Center, University of Minnesota; 2017
# Translated from MATLAB code written by Ian Howat, Ohio State University, 2017


from __future__ import division
import math
import os
import re
from sys import stdout
from warnings import warn

import numpy as np
from scipy import ndimage as sp_ndimage

import raster_array_tools as rat

# TODO: Remove `test` include once testing is complete.
import test


class InvalidArgumentError(Exception):
    def __init__(self, msg):
        super(Exception, self).__init__(msg)

class RasterDimensionError(Exception):
    def __init__(self, msg):
        super(Exception, self).__init__(msg)


def check_arggroups(arggroup_list, check='exist'):
    # TODO: Write docstring.

    check_choices = ('exist', 'full')
    if check not in check_choices:
        raise InvalidArgumentError("`check` must be one of {}, but was '{}'".format(check_choices, check))

    result = []
    for arggroup in arggroup_list:
        ag_check = None
        if type(arggroup) in (list, tuple):
            set_count = [arg is not None for arg in arggroup].count(True)
            if (   (check == 'full'  and set_count == len(arggroup))
                or (check == 'exist' and set_count > 0)):
                ag_check = True
            else:
                ag_check = False
        elif arggroup is not None:
            ag_check = True
        else:
            ag_check = False
        result.append(ag_check)

    return result


def verify_arggroups(arggroup_list):
    # TODO: Write docstring.
    if (   (check_arggroups(arggroup_list, check='exist').count(True) != 1)
        or (check_arggroups(arggroup_list, check='full').count(True)  != 1)):
        return False
    else:
        return True


def generateMasks(demFile, maskFileSuffix, noentropy=False):
    # TODO: Write docstring.

    if maskFileSuffix == 'edgemask/datamask':
        matchFile = demFile.replace('dem.tif', 'matchtag.tif')
        stdout.write(matchFile+"\n")
        mask_v1(matchFile, noentropy)
    else:
        maskFile = demFile.replace('dem.tif', maskFileSuffix+'.tif')
        stdout.write(demFile+"\n")
        mask = None
        if maskFileSuffix == 'mask':
            mask = mask_v2(demFile)
        elif maskFileSuffix == 'mask2a':
            mask = mask_v2a(demFile)
        rat.saveArrayAsTiff(mask, maskFile, like_rasterFile=demFile, nodataVal=0, dtype_out=np.uint8)


def mask_v1(matchFile, noentropy=False):
    """
    Creates edgemask and datamask of the matchtag array,
    with or without entropy protection.

    Parameters
    ----------
    matchFile : str (file path)
        File path of the matchtag image.
    noentropy : bool
        If True, entropy filter is not applied.
        If False, entropy filter is applied.

    Returns
    -------
    void

    Notes
    -----
    Edgemask is saved at matchFile.replace('matchtag.tif', 'edgemask.tif'),
    and likewise with datamask.

    Source file: batch_mask.m
    Source author: Ian Howat, ihowa@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 12/05/17

    """
    # Find SETSM version.
    metaFile = matchFile.replace('matchtag.tif', 'meta.txt')
    setsmVersion = None
    if not os.path.isfile(metaFile):
        print "No meta file, assuming SETSM version > 2.0"
        setsmVersion = 3
    else:
        setsm_pattern = re.compile("SETSM Version=(.*)")
        metaFile_fp = open(metaFile, 'r')
        line = metaFile_fp.readline()
        while line != '':
            match = re.search(setsm_pattern, line)
            if match:
                try:
                    setsmVersion = float('.'.join(match.group(1).strip().split('.')[0:2]))
                except ValueError:
                    setsmVersion = float('inf')
                break
            line = metaFile_fp.readline()
        metaFile_fp.close()
        if setsmVersion is None:
            warn("Missing SETSM Version number in '{}'".format(metaFile))
            # Use settings for default SETSM version.
            setsmVersion = 2.03082016
    print "Using settings for SETSM Version = {}".format(setsmVersion)

    match_array, res = rat.extractRasterParams(matchFile, 'array', 'res')
    match_array = match_array.astype(np.bool)

    if setsmVersion < 2.01292016:
        n = int(math.floor(21*2/res))   # data density kernel window size
        Pmin = 0.8                      # data density threshold for masking
        Amin = int(2000/res)            # minimum data cluster area
        cf = 0.5                        # boundary curvature factor (0 = convex hull, 1 = point boundary)
        crop = n
    else:
        n = int(math.floor(101*2/res))
        Pmin = 0.99
        Amin = int(2000/res)
        cf = 0.5
        crop = n

    edgemask = getDataDensityMask(match_array, kernel_size=n, density_thresh=Pmin)
    if not noentropy:
        entropy_mask = getEntropyMask(matchFile.replace('matchtag.tif', 'ortho.tif'))
        np.logical_or(edgemask, entropy_mask, out=edgemask)
    edgemask = getEdgeMask(edgemask, min_data_cluster=Amin, hull_concavity=cf, crop=crop)
    rat.saveArrayAsTiff(edgemask, matchFile.replace('matchtag.tif', 'edgemask.tif'),
                        like_rasterFile=matchFile, nodataVal=0, dtype_out=np.uint8)

    match_array[~edgemask] = 0
    del edgemask

    # Set datamask filtering parameters based on SETSM version and image resolution.
    if setsmVersion <= 2.0:
        n = int(math.floor(21*2/res))   # data density kernel window size
        Pmin = 0.3                      # data density threshold for masking
        Amin = 1000                     # minimum data cluster area
        Amax = 10000                    # maximum data gap area to leave filled
    else:
        n = int(math.floor(101*2/res))
        Pmin = 0.90
        Amin = 1000
        Amax = 1000

    datamask = getDataDensityMask(match_array, kernel_size=n, density_thresh=Pmin)
    del match_array
    datamask = clean_mask(datamask, remove_pix=Amin, fill_pix=Amax, in_place=True)
    rat.saveArrayAsTiff(datamask, matchFile.replace('matchtag.tif', 'datamask.tif'),
                        like_rasterFile=matchFile, nodataVal=0, dtype_out=np.uint8)


def mask_v2(demFile, avg_kernel_size=21, processing_res=32, min_data_cluster=500):
    # TODO: Write my own docstring.
    """
    % MASK ArcticDEM masking algorithm
    %
    % m = mask(demFile,satID,effectiveBandwidth,abScaleFactor,meanSunElevation)
    % returns the mask stucture (m.x,m.y,m.z) for the demFile using the
    % given image parameters.
    %
    % m = mask(...,maxDigitalNumber,previewPlot) maxDigitalNumber is optional
    % for rescaling the orthoimage to the original source image range.
    % If it's mot included or is empty, no rescaling will be applied. If
    % previewPlot == 'true', a *_maskPreview.tif image will be saved to the
    % same directory as the demFile that shows the DEM hillshade with and
    % without the mask applied.
    %
    % m = mask(demFile,meta) returns the mask stucture (m.x,m.y,m.z) for the
    % demFile and meta structure, where meta is the output of readSceneMeta.
    % Required fields in the meta structure are:
    % 'image_1_satID'
    % 'image_1_wv_correct'
    % 'image_1_effbw'
    % 'image_1_abscalfact'
    % 'image_1_mean_sun_elevation'
    % additionally, if image_1_wv_correct==1, the image_1_max field is also
    % required.
    %
    % REQUIRED FUNCTIONS: readGeotiff, DataDensityMap, rescaleDN, edgeSlopeMask
    % cloudMask, DG_DN2RAD, waterMask
    %
    % Ian Howat, ihowat@gmail.com
    % 25-Jul-2017 12:49:25
    """
    metaFile  = demFile.replace('dem.tif', 'meta.txt')
    matchFile = demFile.replace('dem.tif', 'matchtag.tif')
    orthoFile = demFile.replace('dem.tif', 'ortho.tif')

    meta = readSceneMeta(metaFile)
    satID              = meta['image_1_satID']
    wv_correct_flag    = meta['image_1_wv_correct']
    effbw              = meta['image_1_effbw']
    abscalfact         = meta['image_1_abscalfact']
    mean_sun_elevation = meta['image_1_mean_sun_elevation']
    maxDN = meta['image_1_max'] if wv_correct_flag else None

    # Extract raster data.
    dem_array, image_shape, image_gt = rat.extractRasterParams(demFile, 'array', 'shape', 'geo_trans')
    image_dx = image_gt[1]
    image_dy = image_gt[5]
    image_res = abs(image_dx)
    match_array = rat.extractRasterParams(matchFile, 'array')
    ortho_array = rat.extractRasterParams(orthoFile, 'array')

    # Raster size consistency checks
    if match_array.shape != image_shape:
        raise RasterDimensionError("matchFile '{}' dimensions {} do not match dem dimensions {}".format(
                                   matchFile, match_array.shape, image_shape))

    # FIXME: Mirror functionality from MATLAB code to allow correcting the following dimension error?
    if ortho_array.shape != image_shape:
        raise RasterDimensionError("orthoFile '{}' dimensions {} do not match dem dimensions {}".format(
                                   orthoFile, ortho_array.shape, image_shape))
        # warn("orthoFile '{}' dimensions {} do not match dem dimensions {}".format(
        #     orthoFile, ortho_array.shape, mask_shape))

    dem_nodata = np.isnan(dem_array)  # original background for rescaling
    dem_array[dem_array == -9999] = np.nan
    data_density_map = getDataDensityMap(match_array, avg_kernel_size, conv_depth='single')
    del match_array

    # Re-scale ortho data if WorldView correction is detected in the meta file.
    if maxDN is not None:
        print "rescaled to: 0 to {}".format(maxDN)
        ortho_array = rescaleDN(ortho_array, maxDN)

    # Convert ortho data to radiance.
    ortho_array = DG_DN2RAD(ortho_array, satID=satID, effectiveBandwith=effbw, abscalFactor=abscalfact)
    print "radiance value range: {:.2f} to {:.2f}".format(np.nanmin(ortho_array), np.nanmax(ortho_array))

    # Resize arrays to processing resolution.
    if image_res != processing_res:
        resize_factor = image_res / processing_res
        dem_array        = rat.imresize(dem_array,        resize_factor)
        ortho_array      = rat.imresize(ortho_array,      resize_factor)
        data_density_map = rat.imresize(data_density_map, resize_factor)
        processing_dy, processing_dx = image_res * np.array(image_shape) / np.array(dem_array.shape)
    else:
        processing_dx = processing_res
        processing_dy = processing_res

    # Coordinate ascending/descending directionality affects gradient used in getSlopeMask.
    if image_dx < 0:
        processing_dx = -processing_dx
    if image_dy < 0:
        processing_dy = -processing_dy

    # Set data density map no data.
    data_density_map[np.isnan(dem_array)] = np.nan

    # Mask edges using dem slope.
    mask = getEdgeMask(getSlopeMask(dem_array, dx=processing_dx, dy=processing_dy, source_res=image_res))
    dem_array[~mask] = np.nan
    if not np.any(~np.isnan(dem_array)):
        return mask
    del mask

    # Mask water.
    ortho_array[np.isnan(dem_array)] = 0
    data_density_map[np.isnan(dem_array)] = 0
    mask = getWaterMask(ortho_array, mean_sun_elevation, data_density_map)
    dem_array[~mask] = np.nan
    data_density_map[~mask] = 0
    if not np.any(~np.isnan(dem_array)):
        return mask
    del mask

    # Filter clouds.
    mask = getCloudMask(dem_array, ortho_array, data_density_map)
    dem_array[mask] = np.nan

    # Finalize mask.
    mask = ~np.isnan(dem_array)
    if not np.any(mask):
        return mask
    mask = rat.bwareaopen(mask, min_data_cluster, in_place=True)
    mask = rat.imresize(mask, image_shape, 'nearest')
    mask[dem_nodata] = False

    return mask


def mask_v2a(demFile, avg_kernel_size=5,
             min_data_cluster=10, max_hole_fill=5000,
             cloudthresh_demstdev=0.75, cloudthresh_datadensity=1,
             demstdev_iter_thresh=0.1, iteration_dilate=11,
             dilate_bad=5):
    # TODO: Write my own docstring.
    """
    % remaMask2a mask dem using point density and slopes deviations
    %
    % m = remaMask2a(demFile) masks the dem file by first applying an edgemask
    % and then using an iterative bad pixel search starting from points of low
    % point density and high standard deviation in slope.
    """
    matchFile = demFile.replace('dem.tif', 'matchtag.tif')

    # Read DEM data and extract information for slope/cloud masking.

    dem_array, image_gt = rat.extractRasterParams(demFile, 'array', 'geo_trans')
    image_dx = image_gt[1]
    image_dy = image_gt[5]
    image_res = abs(image_dx)

    if avg_kernel_size is None:
        avg_kernel_size = int(math.floor(21*2/image_res))

    dem_array[dem_array == -9999] = np.nan
    dy, dx = np.gradient(dem_array, image_dy, image_dx)

    # Mask edges using dem slope.
    mask = getEdgeMask(getSlopeMask(dem_array, grad_dx=dx, grad_dy=dy, avg_kernel_size=avg_kernel_size))
    dem_array[~mask] = np.nan
    del mask

    # Iterative expanding matchtag density / slope mask

    dem_nodata = np.isnan(dem_array)
    dx[dem_nodata] = np.nan
    dy[dem_nodata] = np.nan

    avg_kernel = np.ones((avg_kernel_size, avg_kernel_size), dtype=np.float32)

    dk_list = [dx, dy]
    dk_nodata_list = []
    stdev_dk_list = []
    for dk in dk_list:
        dk_nodata = np.isnan(dk)
        dk[dk_nodata] = 0
        mean_dk = rat.moving_average(dk, kernel=avg_kernel)
        stdev_dk = rat.moving_average(np.square(dk), kernel=avg_kernel) - np.square(mean_dk)
        stdev_dk[stdev_dk < 0] = 0
        stdev_dk = np.sqrt(stdev_dk)
        dk_nodata_list.append(dk_nodata)
        stdev_dk_list.append(stdev_dk)
    del dk_list, dx, dy, dk, dk_nodata, mean_dk, stdev_dk

    stdev_elev_array = np.sqrt(np.square(stdev_dk_list[0]) + np.square(stdev_dk_list[1]))
    stdev_elev_nodata = rat.imdilate(dk_nodata_list[0] | dk_nodata_list[1], structure=avg_kernel)
    stdev_elev_array[stdev_elev_nodata] = np.nan
    del stdev_dk_list, dk_nodata_list

    # Read matchtag and make data density map.
    match_array = rat.extractRasterParams(matchFile, 'array')
    data_density_map = getDataDensityMap(match_array, avg_kernel_size, conv_depth='single')
    data_density_map[dem_nodata] = np.nan

    # Locate probable cloud pixels.
    mask = (stdev_elev_array > cloudthresh_demstdev) & (data_density_map < cloudthresh_datadensity)

    # Remove small data clusters.
    mask = rat.bwareaopen(mask, min_data_cluster, in_place=True)

    # Initialize masked pixel counters.
    N0 = np.count_nonzero(mask)
    N1 = np.inf

    # Background mask
    mask_bkg = dem_nodata | stdev_elev_nodata

    # Expand mask to surrounding bad pixels,
    # stop when mask stops growing.
    dilate_structure = np.ones((iteration_dilate, iteration_dilate), dtype=np.uint8)
    while N0 != N1:
        N0 = N1  # Set new to old.
        mask = rat.imdilate(mask, dilate_structure)  # Dilate the mask.
        mask[mask_bkg | (stdev_elev_array < demstdev_iter_thresh)] = False
        N1 = np.count_nonzero(mask)

    # Remove small data gaps.
    mask = ~rat.bwareaopen(~mask, max_hole_fill, in_place=True)

    # Remove border effect.
    mask = mask | rat.imdilate(dem_nodata, size=dilate_bad)

    # remove small data gaps.
    mask = ~rat.bwareaopen(~mask, max_hole_fill, in_place=True)

    return ~mask


def getDataDensityMap(array, kernel_size=11, conv_depth='double'):
    # TODO: Write docstring.
    return rat.moving_average(array, kernel_size, shape='same', conv_depth=conv_depth)


def getDataDensityMask(match_array, kernel_size=21, density_thresh=0.3, conv_depth='single'):
    """
    Return an array masking off areas of poor data coverage in a matchtag array.

    Parameters
    ----------
    match_array : ndarray, 2D
        Binary array to mask containing locations of good data values.
    kernel_size : positive int
        Side length of the neighborhood to use for calculating data density fraction.
    density_thresh : positive float
        Minimum data density fraction for a pixel to be set to 1 in the mask.

    Returns
    -------
    getDataDensityMask : ndarray of bool, same shape as data_array
        The data density mask of the input matchtag array.

    Notes
    -----
    *Source file: DataDensityMask.m
    Source author: Ian Howat, ihowa@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 12/05/17

    *Functionality has been modified in translation:
        - Removal of small data clusters and small data gaps.
        To replicate functionality of DataDensityMask.m, pass the result of this function to clean_mask().

    """
    return getDataDensityMap(match_array, kernel_size, conv_depth) >= density_thresh


def getEntropyMask(orthoFile,
                   entropy_thresh=0.2, min_data_cluster=1000,
                   processing_res=8, kernel_size=None):
    """
    Return array masking off areas of low entropy, such as water, in an orthorectified image.

    Parameters
    ----------
    orthoFile : str (file path)
        File path of the ortho image to process.
    entropy_thresh : positive float
        Minimum entropy threshold. 0.2 seems to be good for water.
    min_data_cluster : positive int
        Minimum number of contiguous data pixels in a kept data cluster.
    processing_res : positive float (meters)
        Resample ortho image to this resolution for processing for speed and smooth.
    kernel_size : None or positive int
        Side length of square neighborhood (of ones)
        to be used as kernel for entropy filter.
        If None, is set automatically by processing_res.

    Returns
    -------
    getEntropyMask : ndarray of type bool, same shape as ortho image
        The entropy mask masking off areas of low entropy in input orthoFile image.

    Notes
    -----
    Source file: entropyMask.m
    Source author: Ian Howat, ihowa@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 12/05/17

    Source docstring:
    % entropyMask classify areas of low entropy in an image such as water
    %
    % M = entropyMask(orthoFile) returns the low entropy classification mask
    % from the geotif image in orthoFile. Also checks whether wvc was applied
    % from the metafile.
    %
    % Ian Howat,ihowat@gmail.com, Ohio State
    % 13-Apr-2017 10:41:41

    """
    if kernel_size is None:
        kernel_size = int(math.floor(21*2/processing_res))

    metaFile = orthoFile.replace('ortho.tif', 'meta.txt')
    wvcFlag = False
    if not os.path.isfile(metaFile):
        warn("no meta file found, assuming no wv_correct applied")
    else:
        meta = readSceneMeta(metaFile)
        wvcFlag = meta['image_1_wv_correct']
        if wvcFlag:
            print "wv_correct applied"
        else:
            print "wv_correct not applied"

    # Read ortho.
    ortho_array, image_shape, image_res = rat.extractRasterParams(orthoFile, 'array', 'shape', 'res')

    background_mask = (ortho_array == 0)  # image background mask

    # Resize ortho to pres.
    if image_res != processing_res:
        ortho_array = rat.imresize(ortho_array, image_res/processing_res)

    # Subtraction image
    ortho_subtraction = (  sp_ndimage.maximum_filter1d(ortho_array, kernel_size, axis=0)
                         - sp_ndimage.minimum_filter1d(ortho_array, kernel_size, axis=0))
    if not wvcFlag:
        ortho_subtraction = ortho_subtraction.astype(np.uint8)

    # Entropy image
    entropy_array = rat.entropyfilt(ortho_subtraction, np.ones((kernel_size, kernel_size)))
    mask = (entropy_array < entropy_thresh)
    del entropy_array

    mask = clean_mask(mask, remove_pix=min_data_cluster, fill_pix=min_data_cluster, in_place=True)

    # Resize ortho to 8m.
    if image_res != processing_res:
        mask = rat.imresize(mask, image_shape, 'nearest')

    mask[background_mask] = False

    return mask


def getSlopeMask(dem_array,
                 res=None,
                 dx=None, dy=None,
                 X=None, Y=None,
                 grad_dx=None, grad_dy=None,
                 source_res=None, avg_kernel_size=None,
                 dilate_bad=13):
    """
    Return an array masking off artifacts with high slope values in a DEM array.

    Parameters
    ----------
    dem_array : ndarray, 2D
        Array containing floating point DEM data.
    res : None or positive float (meters)
        Square resolution of pixels in `dem_array`.
    dx : None or positive int
        Horizontal length of pixels in `dem_array`.
    dy : None or positive int
        Vertical length of pixels in `dem_array`.
    X : None or (ndarray, 1D)
        x-axis coordinate vector for `dem_array`.
    Y : None or (darray, 1D)
        y-axis coordinate vector for `dem_array`.
    grad_dx : None or (ndarray, 2D, shape like `dem_array`)
        x-axis gradient of `dem_array`.
    grad_dy : None or (ndarray, 2D, shape like `dem_array`)
        y-axis gradient of `dem_array`.
    source_res : positive float (meters)
        Square resolution of pixels in the source image.
    avg_kernel_size : None or positive int
        Side length of square neighborhood (of ones)
        to be used as kernel for calculating mean slope.
        If None, is set automatically by `source_res`.
    dilate_bad : None or positive int
        Side length of square neighborhood (of ones)
        to be used as kernel for dilating masked pixels.

    Returns
    -------
    getSlopeMask : ndarray of type bool, same shape as dem_array
        The slope mask masking off artifacts with high slope values in input dem_array.

    Notes
    -----
    Provide one of (x and y coordinate vectors x_dem and y_dem), input image resolution input_res,
    (pre-calculated x and y gradient arrays dx and dy).
    Note that y_dem and x_dem must both have the same uniform coordinate spacing AS WELL AS being
    both in increasing or both in decreasing order for the results of np.gradient(dem_array, y_dem, x_dem)
    to be equal to the results of np.gradient(dem_array, input_res), with input_res being a positive
    number for increasing order or a negative number for decreasing order.

    The returned mask sets to 1 all pixels for which the surrounding [kernel_size x kernel_size]
    neighborhood has an average slope greater than 1, then erode it by a kernel of ones with side
    length dilate_bad.

    *Source file: edgeSlopeMask.m
    Source author: Ian Howat, ihowa@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 10/17/17

    Source docstring:
    % ESDGESLOPEMASK mask artificats on edges of DEM using high slope values
    %
    % M = edgeSlopeMask(x,y,z) masks bad edge values in the DEM with coordinate
    % vectors x any y.
    %
    % [M,dx,dy] = edgeSlopeMask(x,y,z) returns the gradient arrays
    %
    % Ian Howat, ihowat@gmail.com
    % 24-Jul-2017 15:39:07
    % 04-Oct-2017 15:19:47: Added option/value argument support

    *Functionality has been modified in translation:
        - Removal of edge masking.
        To replicate functionality of edgeSlopeMask.m, pass the result of this function to getEdgeMask().

    """
    if not verify_arggroups((res, (dx, dy), (X, Y), (grad_dx, grad_dy))):
        raise InvalidArgumentError(
            "One type of pixel spacing input ([full regular `res`], [regular x `dx`, regular y `dy`]  "
            "[x and y coordinate arrays `X` and `Y`], or [x and y gradient 2D arrays `grad_dx` and `grad_dy`]) "
            "must be provided"
        )
    if avg_kernel_size is None:
        avg_kernel_size = int(math.floor(21*2/source_res))

    # Get elevation grade at each pixel.
    if grad_dx is None:
        if res is not None:
            grad_dy, grad_dx = np.gradient(dem_array, res)
        elif dx is not None:
            grad_dy, grad_dx = np.gradient(dem_array, dy, dx)
        elif X is not None:
            grad_dy, grad_dx = np.gradient(dem_array, Y, X)
    grade = np.sqrt(np.square(grad_dx) + np.square(grad_dy))

    # Mean grade over n-pixel kernel
    mean_slope_array = rat.moving_average(grade, avg_kernel_size, conv_depth='single')

    # Mask mean slopes greater than 1.
    mask = mean_slope_array < 1

    if dilate_bad is not None:
        # Dilate high mean slope pixels and set to false.
        mask[rat.imdilate((mean_slope_array > 1), size=dilate_bad)] = False

    return mask


def getWaterMask(ortho_array, meanSunElevation, data_density_map,
                 sunElevation_split=30, ortho_thresh_lowsunelev=5, ortho_thresh_highsunelev=20,
                 entropy_thresh=0.2, data_density_thresh=0.98, min_data_cluster=500,
                 kernel_size=5, dilate=7):
    # TODO: Write my own docstring.
    """
    Source file: waterMask.m
    Source author: Ian Howat, ihowa@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 10/19/17
    """
    ortho_thresh = ortho_thresh_lowsunelev if meanSunElevation < sunElevation_split else ortho_thresh_highsunelev

    # Subtraction image
    ortho_subtraction = (  sp_ndimage.maximum_filter1d(ortho_array, kernel_size, axis=0)
                         - sp_ndimage.minimum_filter1d(ortho_array, kernel_size, axis=0))

    # Entropy image
    entropy_array = rat.entropyfilt(rat.astype_matlab(ortho_subtraction, np.uint8),
                                    np.ones((kernel_size, kernel_size)))

    # Set edge-effected values to zero.
    entropy_array[ortho_array == 0] = 0

    # Mask data with entropy less than threshold.
    entropy_mask = ((ortho_array != 0) & (entropy_array < entropy_thresh))

    # Remove isolated clusters of masked pixels.
    entropy_mask = rat.bwareaopen(entropy_mask, min_data_cluster, in_place=True)

    # Dilate masked pixels.
    entropy_mask = rat.imdilate(entropy_mask, size=dilate)

    # Mask data with low radiance and matchpoint density.
    radiance_mask = ((ortho_array != 0) & (ortho_array < ortho_thresh) & (data_density_map < data_density_thresh))

    # Remove isolated clusters of masked pixels.
    radiance_mask = rat.bwareaopen(radiance_mask, min_data_cluster, in_place=True)

    # Assemble water mask.
    mask = (~entropy_mask & ~radiance_mask & (ortho_array != 0))

    # Remove isolated clusters of data.
    mask = clean_mask(mask, remove_pix=min_data_cluster, fill_pix=min_data_cluster, in_place=True)

    return mask


def getCloudMask(dem_array, ortho_array, data_density_map,
                 elevation_percentile_split=80, ortho_thresh=70,
                 data_density_thresh_lorad=0.6, data_density_thresh_hirad=0.9,
                 min_data_cluster=10000, min_nodata_cluster=1000,
                 avg_kernel_size=21, dilate_bad=21,
                 erode_border=31, dilate_border=61):
    # TODO: Write my own docstring.
    """
    Source file: cloudMask.m
    Source author: Ian Howat, ihowa@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 10/19/17

    Source docstring:
    % cloudMask mask bad surfaces on DEM based on slope and radiance

    % M = cloudMask(z,or)masks bad edge values in the DEM with coordinate
    % vectors x any y.
    %
    % Ian Howat, ihowat@gmail.com
    % 24-Jul-2017 15:39:07
    """

    # Make sure sufficient non NaN pixels exist, otherwise cut to the chase.
    if np.count_nonzero(~np.isnan(dem_array)) < 2*min_nodata_cluster:
        mask = np.ones(dem_array.shape, dtype=np.bool)
        return mask

    # Calculate standard deviation of elevation.
    mean_elev_array = rat.moving_average(dem_array, avg_kernel_size, shape='same', conv_depth='single')
    stdev_elev_array = (rat.moving_average(np.square(dem_array), avg_kernel_size, shape='same', conv_depth='single')
                        - np.square(mean_elev_array))
    stdev_elev_array[stdev_elev_array < 0] = 0
    stdev_elev_array = np.sqrt(stdev_elev_array)

    # Calculate elevation percentile difference.
    percentile_diff = (  np.nanpercentile(dem_array, elevation_percentile_split)
                       - np.nanpercentile(dem_array, 100 - elevation_percentile_split))

    # Set standard deviation difference based on percentile difference.
    stdev_thresh = None
    if percentile_diff <= 40:
        stdev_thresh = 10.5
    elif 40 < percentile_diff <= 50:
        stdev_thresh = 15
    elif 50 < percentile_diff <= 75:
        stdev_thresh = 19
    elif 75 < percentile_diff <= 100:
        stdev_thresh = 27
    elif percentile_diff > 100:
        stdev_thresh = 50

    print "{}/{} percentile elevation difference: {:.1f}, sigma-z threshold: {:.1f}".format(
        100 - elevation_percentile_split, elevation_percentile_split, percentile_diff, stdev_thresh
    )

    # Apply mask conditions.
    mask = (~np.isnan(dem_array)
            & (((ortho_array > ortho_thresh) & (data_density_map < data_density_thresh_hirad))
                | (data_density_map < data_density_thresh_lorad)
                | (stdev_elev_array > stdev_thresh)))

    # Fill holes in masked clusters.
    mask = sp_ndimage.morphology.binary_fill_holes(mask)

    # Remove small masked clusters.
    mask = rat.bwareaopen(mask, min_nodata_cluster, in_place=True)

    # Remove thin borders caused by cliffs/ridges.
    mask_edge = rat.imerode(mask, size=erode_border)
    mask_edge = rat.imdilate(mask_edge, size=dilate_border)

    mask = (mask & mask_edge)

    # Dilate nodata.
    mask = rat.imdilate(mask, size=dilate_bad)

    # Remove small clusters of unfiltered data.
    mask = ~rat.bwareaopen(~mask, min_data_cluster, in_place=True)

    return mask


def getEdgeMask(match_array, hull_concavity=0.5, crop=None,
                res=None, min_data_cluster=1000,):
    """
    Return an array masking off bad edges of a "mass" of data (see notes) in a matchtag array.

    Parameters
    ----------
    match_array : ndarray, 2D
        Binary array to mask containing locations of good data values.
    hull_concavity : 0 <= float <= 1
        Boundary curvature factor argument be passed to concave_hull_image().
        (0 = convex hull, 1 = point boundary)
    crop : None or positive int
        Erode the mask by a square neighborhood (ones) of this side length before return.
    res : positive int
        Image resolution corresponding to data_array, for setting parameter default values.
    min_data_cluster : None or positive int
        Minimum number of contiguous data pixels in a kept data cluster.
        If None, is set automatically by res.
    data_boundary_res : positive int
        Data boundary resolution to be passed to concave_hull_image().

    Returns
    -------
    getEdgeMask : ndarray of bool, same shape as data_array
        The edge mask masking off bad data hull edges in input match_array.

    See also
    --------
    concave_hull_image

    Notes
    -----
    The input array is presumed to contain a large "mass" of data (non-zero) values near its center,
    which may or may not have holes.
    The returned mask discards all area outside of the (convex) hull of the region containing both
    the data mass and all data clusters of more pixels than min_data_cluster.
    Either res or min_data_cluster must be provided.

    *Source file: edgeMask.m
    Source author: Ian Howat, ihowat@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 10/17/17

    Source docstring:
    % edgeMask returns mask for bad edges using the matchtag field
    %
    % m1 = edgeMask(m0) where m0 is the matchtag array returns a binary mask of
    % size(m0) designed to filter data bad edges using match point density

    *Functionality has been modified in translation:
        - Removal of data density masking.
        - Removal of entropy masking.
        To replicate functionality of edgeMask.m, do masking of data_array with getDataDensityMask()
        and getEntropyMask() before passing the result to this function.

    """
    if res is None and min_data_cluster is None:
        raise InvalidArgumentError("Resolution `res` argument must be provided "
                                   "to set default values of min_data_cluster")
    if not np.any(match_array):
        return match_array.astype(np.bool)
    if min_data_cluster is None:
        min_data_cluster = int(math.floor(1000*2/res))

    # Fill interior holes since we're just looking for edges here.
    mask = sp_ndimage.morphology.binary_fill_holes(match_array)
    # Get rid of isolated little clusters of data.
    mask = rat.bwareaopen(mask, min_data_cluster, in_place=True)

    if not np.any(mask):
        # No clusters exceed minimum cluster area.
        return mask

    mask = rat.concave_hull_image(mask, hull_concavity)

    if crop is not None:
        mask = rat.imerode(mask, size=crop)

    return mask


def clean_mask(mask, remove_pix=1000, fill_pix=10000, in_place=False):
    """
    Remove small clusters of "data" ones and fill small holes of "no-data" zeros in a mask array.

    Parameters
    ----------
    mask : ndarray, 2D
        Binary array to mask containing locations of good data values.
    remove_pix : positive int
        Minimum number of contiguous one pixels in a kept data cluster.
    fill_pix : positive int
        Maximum number of contiguous zero pixels in a filled data void.
    in_place : bool
        If `True`, remove the clean the mask in the input array itself.
        Otherwise, make a copy.

    Returns
    -------
    clean_mask : ndarray of type bool, same shape as data_array
        The cluster and hole mask of the input matchtag array.

    """
    if not np.any(mask):
        return mask.astype(np.bool)

    # Remove small data clusters.
    cleaned_mask = rat.bwareaopen(mask, remove_pix, in_place=in_place)
    # Fill small data voids.
    return ~rat.bwareaopen(~cleaned_mask, fill_pix, in_place=True)


def readSceneMeta(metaFile):
    # TODO: Write my own docstring.
    """
    Source file: readSceneMeta.m
    Source author: Ian Howat, ihowat@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 10/20/17

    Source docstring:
    %READMETA read SETSM output metafile for an individual DEM
    %
    % meta=readSceneMeta(metaFile) reads each line of the metaFile into the
    % structure meta. The field neames are the same as in the metaFile but with
    % underscores for spaces and all lowercase.
    %
    % Ian Howat, ihowat@gmail.com
    % 24-Jul-2017 14:57:49
    """
    meta = {}
    metaFile_fp = open(metaFile, 'r')
    line = metaFile_fp.readline()
    while line != '':
        equal_index = line.find('=')
        if equal_index != -1:
            field_name = line[:equal_index].strip().replace(' ', '_').lower()
            if field_name in meta:
                meta['image_1_'+field_name] = meta.pop(field_name)
                field_name = 'image_2_'+field_name
            field_value = line[(equal_index+1):].strip()
            try:
                field_value = float(field_value)
            except ValueError:
                pass
            meta[field_name] = field_value
        line = metaFile_fp.readline()
    metaFile_fp.close()

    # Get satID and check for cross track naming convention.
    satID = os.path.basename(meta['image_1'])[0:4].upper()
    satID_abbrev = satID[0:2]
    if   satID_abbrev == 'W1':
        satID = 'WV01'
    elif satID_abbrev == 'W2':
        satID = 'WV02'
    elif satID_abbrev == 'W3':
        satID = 'WV03'
    elif satID_abbrev == 'G1':
        satID = 'GE01'
    elif satID_abbrev == 'Q1':
        satID = 'QB01'
    elif satID_abbrev == 'Q2':
        satID = 'QB02'
    elif satID_abbrev == 'I1':
        satID = 'IK01'
    meta['image_1_satID'] = satID

    return meta


def rescaleDN(ortho_array, dnmax):
    # TODO: Write my own docstring.
    """
    Source file: rescaleDN.m
    Source author: Ian Howat, ihowat@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 10/23/17

    Source docstring:
    % RESCALEDN rescale digitial numbers to new maximum
    %
    % dn=rescaleDN(dn,dnmax)rescales the digital number image dn to the new
    % maximum in dnmax.
    %
    % Ian Howat, ihowat@gmail.com
    % 24-Jul-2017 15:50:25
    """
    # Set the minimum and maximum values of this scale.
    # We use a fixed scale because this is what all data is scaled to after application of
    # wv_correct regardless of actual min or max.
    ormin = 0
    ormax = 32767

    # Set the new minimum and maximum.
    # dnmin is zero because nodata is apparently used in the scaling.
    dnmin = 0
    dnmax = float(dnmax)

    # Rescale back to original dn.
    return dnmin + (dnmax-dnmin)*(ortho_array.astype(np.float32) - ormin)/(ormax-ormin)


def DG_DN2RAD(DN,
              xmlFile=None,
              satID=None, effectiveBandwith=None, abscalFactor=None):
    # TODO: Write my own docstring.
    """
    Source file: DG_DN2RAD.m
    Source author: Ian Howat, ihowat@gmail.com, Ohio State University
    Source repo: setsm_postprocessing, branch "3.0" (GitHub)
    Translation date: 10/23/17

    Source docstring:
    % DG_DN2RAD converts DG DN images to top-of-atmosphere radiance
    %
    % L =  = DG_DN2RAD(DN, satID, effectiveBandwith, abscalFactor) applies the
    % conversion using the supplied factors with a table look-up for the satID.
    % The output L is top-of-atmosphere radiance in units of Wµm^-1 m^-2 sr^-1.
    %
    % L =  = DG_DN2RAD(DN,xmlFile) reads the factors from the supplied xml file
    %
    % [L, effectiveBandwith, abscalFactor, gain, offset] = DG_DN2RAD(...)
    % returns the scaling parameters used.
    """
    xml_params = [
        [satID, 'SATID'],
        [effectiveBandwith, 'EFFECTIVEBANDWIDTH'],
        [abscalFactor, 'ABSCALFACTOR']
    ]
    if None in [p[0] for p in xml_params]:
        if xmlFile is None:
            raise InvalidArgumentError("`xmlFile` argument must be given to automatically set xml params")
        fillMissingXmlParams(xmlFile, xml_params)
        satID, effectiveBandwith, abscalFactor = [p[0] for p in xml_params]
        effectiveBandwith = float(effectiveBandwith)
        abscalFactor = float(abscalFactor)

    # Values from:
    # https://dg-cms-uploads-production.s3.amazonaws.com/uploads/document/file/209/DGConstellationAbsRadCalAdjustmentFactors_2015v2.pdf
    sensor = ('WV03',   'WV02', 'GE01', 'QB2',  'IKO',  'WV01')
    gain   = (0.923,    0.96,   0.978,  0.876,  0.907,  1.016)
    offset = [-1.7,     -2.957, -1.948, -2.157, -4.461, -3.932]

    sensor_index = sensor.index(satID)
    gain = gain[sensor_index]
    offset = offset[sensor_index]

    DN = DN.astype(np.float32)
    DN[DN == 0] = np.nan

    # Calculate radiance.
    return gain*DN*(abscalFactor/effectiveBandwith) + offset


def fillMissingXmlParams(xmlFile, xml_params):
    # TODO: Write docstring.
    xml_paramstrs = [p[1] for p in xml_params]
    xml_paramstrs_to_read = [p[1] for p in xml_params if p[0] is None]
    for paramstr, paramval in zip(xml_paramstrs_to_read, readFromXml(xmlFile, xml_paramstrs_to_read)):
        xml_params[xml_paramstrs.index(paramstr)][0] = paramval


def readFromXml(xmlFile, xml_paramstrs):
    # TODO: Write docstring.
    xml_paramstrs_left = list(xml_paramstrs)
    values = [None]*len(xml_paramstrs)
    xmlFile_fp = open(xmlFile, 'r')
    line = xmlFile_fp.readline()
    while line != '' and None in values:
        for ps in xml_paramstrs_left:
            if ps in line:
                values[xml_paramstrs.index(ps)] = line.replace("<{}>".format(ps), '').replace("</{}>".format(ps), '')
                xml_paramstrs_left.remove(ps)
                break
    xmlFile_fp.close()
    return values
