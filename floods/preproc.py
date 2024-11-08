import json
import logging
import os
from glob import glob
from pathlib import Path
from typing import Callable, Counter, Optional, Set, Tuple, Union

import cv2
import numpy as np
import rasterio
from joblib import Parallel, delayed
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.windows import Window
from skimage.restoration import denoise_nl_means
from tqdm import tqdm

from floods.config.preproc import ImageType, PreparationConfig, StatsConfig
from floods.datasets.flood import FloodDataset
from floods.utils.common import check_or_make_dir, print_config
from floods.utils.gis import imread, mask_raster, write_window
from floods.utils.ml import F16_EPS, identity
from floods.utils.tiling import DynamicOverlapTiler, SingleImageTiler, Tiler

LOG = logging.getLogger(__name__)


class MorphologyTransform:
    """Callable operator that applies morphological transforms to the masks.
    """
    def __init__(self, kernel_size: int = 5, channels_first: bool = True) -> None:
        self.kernel = self.create_round_kernel(kernel_size=kernel_size)
        self.channels_first = channels_first

    def create_round_kernel(self, kernel_size: int):
        # compute center and radius, suppose symmetrical and centered
        center = kernel_size // 2
        radius = min(center, kernel_size - center)
        # compute a distance grid from the given center
        yy, xx = np.ogrid[:kernel_size, :kernel_size]
        dist_from_center = np.sqrt((xx - center)**2 + (yy - center)**2)
        # produce a binary mask
        mask = dist_from_center <= radius
        return mask.astype(np.uint8)

    def _process_mask(self, image: np.ndarray):
        # we cannot deal with channels-first images, bring them to the end
        if self.channels_first:
            image = image.transpose(1, 2, 0)
        assert image.ndim == 2 or image.shape[-1] == 1, "Greyscale images only"
        # a first round of opening to remove noise, followed by closing to fill holes
        # result = cv2.morphologyEx(image, cv2.MORPH_OPEN, self.kernel)
        result = cv2.morphologyEx(image, cv2.MORPH_CLOSE, self.kernel)
        result = np.expand_dims(result, axis=0 if self.channels_first else -1)
        return (result > 0).astype(np.uint8)

    def __call__(self, image: np.ndarray) -> np.ndarray:
        return self._process_mask(image)


def _dims(image: np.ndarray) -> Tuple[int, int]:
    """Returns the first two dimensions of the given array, supposed to be
    an image in channels-last configuration (width - height).

    Args:
        image (np.ndarray): array representing an image

    Returns:
        Tuple[int, int]: height and width
    """
    return image.shape[:2]


def _extract_emsr(path: Union[str, Path]) -> str:
    """Transforms string like 'EMSR345-0-0' into 'EMSR345'

    Args:
        image_id (str): image stem

    Returns:
        str: EMSR code of the image
    """
    image_id = Path(path).stem
    return image_id.split("-")[0]


def _delete_group(*paths: Tuple[Path, ...]) -> None:
    """Deletes the given variable list of paths, useful to delete tuples.
    """
    for path in paths:
        if os.path.exists(str(path)):
            os.remove(path)


def _gather_files(sar_glob: Path,
                  dem_glob: Path,
                  mask_glob: Path,
                  check_stems: bool = True,
                  subset: Set[str] = None) -> Tuple[list, list, list]:
    """Gathers files from the given glob definitions, then checks for consistency.

    Args:
        sar_glob (Path): string or path in glob format (folder/*.tif or something)
        dem_glob (Path): same but for DEM images
        mask_glob (Path): same but for mask images
        check_stems (bool, optional): whether to check also image names or not. Defaults to True.

    Returns:
        Tuple[list, list, list]: tuple of lists, with matching paths at each index i
    """
    # Find images and sanity checks
    sar_files = sorted(glob(str(sar_glob)))
    dem_files = sorted(glob(str(dem_glob)))
    msk_files = sorted(glob(str(mask_glob)))
    # assert that:
    # 1) first of all, we found images
    # 2) we found the same amount of sar, dem and masks
    # 3) once in the same order, the image IDs correspond
    assert len(sar_files) > 0, f"no files found for: {str(sar_glob)}"
    assert (len(sar_files) == len(msk_files) == len(dem_files)
            ), f"Mismatching counts, SAR: {len(sar_files)} - DEM: {len(dem_files)}, - GT: {len(msk_files)}"
    if check_stems:
        for paths in zip(sar_files, dem_files, msk_files):
            names = [Path(p).stem for p in paths]
            assert names.count(names[0]) == len(names), f"Name mismatch in tuple: {names}"
    if subset:
        sar_files = list(filter(lambda p: _extract_emsr(p) in subset, sar_files))
        dem_files = list(filter(lambda p: _extract_emsr(p) in subset, dem_files))
        msk_files = list(filter(lambda p: _extract_emsr(p) in subset, msk_files))

    return sar_files, dem_files, msk_files


def _decibel(data: np.ndarray):
    """
        SentinelHub had the following preprocess:
        return np.maximum(0, np.log10(data + F16_EPS) * 0.21714724095 + 1)
        Changed to custom to the following (for better distribution of pixels + cap)
    """
    # np.clip( , a_min = -50, a_max=0)
    return np.log10(1 + data + F16_EPS)


def _rgb_ratio(data: np.ndarray):
    """False-color RGB formula taken directly from Sentinel-hub.
    """
    vv, vh = data[0], data[1]
    r = vv / 0.28
    g = vh / 0.06
    b = vh / vv / 0.49
    return np.clip(np.stack((r, g, b), axis=0), 0, 1)


def _clip_dem(data: np.ndarray):
    """
        MinMax the DEM between -100 and 6000 meters
    """
    data[data < -100] = -100
    data[data > 6000] = 6000
    return data


def _process_tiff(image_id: str,
                  source_path: Path,
                  dst_path: Path,
                  image_type: ImageType,
                  tiling_fn: Tiler,
                  process_fn: Optional[Callable] = None,
                  scale: float = 1.0,
                  resampling: Resampling = Resampling.bilinear,
                  is_context: bool = False,
                  name_suffix: str = "") -> Tuple[int, int]:
    """Main function, processes the inputs by reading them, doing some checks and additional stuff
    for masks, then tiles them ans stores the single chips.

    Args:
        image_id (str): emsr-like code identifier of the tuple.
        source_path (Path): path to the image.
        dst_path (Path): path where to store the tiles.
        image_type (ImageType): sar, dem or mask.
        tiling_fn (Callable): callable for the tiling operation, yields coordinates.
        process_fn (Optional[Callable], optional): optional callable for mask processing. Defaults to None.
        scale (Optional[float]): optional value to up/downscale the image before tiling.
        resampling (Optional[Resampling]): resampling strategy, defaults to bilinear.
        is_context (Optional[bool]): whether the current image is a context image (should be shrinked).
        name_suffix (Optional[str]): optional suffix to add at the end of the file (useful for multi-scale).

    Returns:
        Tuple[int, int]: number of tiles for each axis
    """
    group, _ = image_type.value
    process_fn = process_fn or identity
    # create destination subfolders paths (dem/sar/mask) and additional context folder if necessary
    ctx_dir = "context" if is_context else ""
    root_dir = Path(dst_path) / group / ctx_dir
    check_or_make_dir(root_dir)
    # read image using rasterio
    # once opened, rescale according to the factor, adjusting the transform
    with rasterio.open(str(source_path), mode="r", driver="GTiff") as dataset:
        # read image with varying output shape to resize
        # 1. if multiscale, resize proportionally
        # 2. if context, shrink the whole image to the tile dimension
        orig_height = dataset.height
        orig_width = dataset.width
        if is_context:
            out_shape = (dataset.count, tiling_fn.tile_size, tiling_fn.tile_size)
        else:
            out_shape = (dataset.count, int(dataset.height * scale), int(dataset.width * scale))
        image = dataset.read(out_shape=out_shape, resampling=resampling)
        _, height, width = image.shape
        # given the possible resize, both transform and dimensions need to be updated
        transform = dataset.transform * dataset.transform.scale(dataset.width / width, dataset.height / height)
        profile: dict = dataset.profile
        # transform the image to extract the number of channels (it may vary depending on process_fn)
        processed_img = process_fn(image)
        channels = processed_img.shape[0]
        profile.update(height=height, width=width, count=channels)
        # create and open an in-memory file to store results
        # during preprocessing
        with MemoryFile() as memfile:
            with memfile.open(**profile) as processed:
                processed.write(processed_img)
                processed.transform = transform
                generator = tiling_fn(image)
                # store result, using target raster (which could be either)
                for (tile_row, tile_col), coords in generator:
                    x1, y1, x2, y2 = coords
                    window = Window.from_slices(rows=(x1, x2), cols=(y1, y2))
                    if is_context:
                        tile_path = root_dir / f"{image_id}_{orig_height}_{orig_width}{name_suffix}.tif"
                    else:
                        tile_path = root_dir / f"{image_id}_{x1}_{y1}{name_suffix}.tif"
                    write_window(window, processed, path=tile_path)
    # return the total amount of tile rows and cols and a mask, if present
    return tile_row + 1, tile_col + 1


def preprocess_data(config: PreparationConfig):
    # A couple prints just to be 😎
    LOG.info("Preparing dataset...")
    print_config(LOG, config)

    # initial checks and common folder inits (if not exists, create)
    dst_dir = check_or_make_dir(config.data_processed)
    code2subset = dict()
    # load the summary file containing all the EMSR information, including splits
    with open(config.summary_file, "r", encoding="utf-8") as f:
        for k, v in json.load(f).items():
            code2subset[k] = v["subset"]

    LOG.info(f"EMSR activations: {dict(Counter(code2subset.values()))}")

    for subset in config.subset:
        emsr_codes = {k for k, v in code2subset.items() if v == subset}
        subset_dir = check_or_make_dir(dst_dir / subset)
        is_test_set = subset == "test"

        if config.tiling:
            # Find images and sanity checks
            LOG.info(f"Processing {subset} set...")
            sar_files, dem_files, msk_files = _gather_files(sar_glob=config.data_source / "*" / "s1_raw" / "*.tif",
                                                            dem_glob=config.data_source / "*" / "DEM" / "*.tif",
                                                            mask_glob=config.data_source / "*" / "mask" / "*.tif",
                                                            subset=emsr_codes)
            LOG.info(f"Raw dataset: {len(sar_files)} images")
            # prepare processing instances
            # actual tiling is done only on train and validation sets,
            # test images are kept as-is, will be tiled during the testing phase
            if not is_test_set:
                tiler = DynamicOverlapTiler(tile_size=config.tile_size,
                                            overlap_threshold=config.tile_max_overlap,
                                            channels_first=True)
                available_scales = config.scale
                make_context = config.make_context
            else:
                available_scales = [1]
                tiler = SingleImageTiler(tile_size=config.tile_size, channels_first=True)
            LOG.info(f"Tiling with {type(tiler).__name__}")
            # prepare preprocessing functions
            sar_process = None
            if config.decibel:
                sar_process = _decibel
            elif config.colorize:
                sar_process = _rgb_ratio
            dem_process = _clip_dem if config.clip_dem else None
            morph = None if not config.morphology else MorphologyTransform(kernel_size=config.morph_kernel,
                                                                           channels_first=True)
            # iterate over the different required scales
            # values are reversed: a scale factor of 2 (x2) means a tile 1024x1024
            # this is equivalent to a tile 512x512, on the image downscaled by 1/2
            for tile_scale in available_scales:
                LOG.info(f"Processing raw dataset with scale: x{tile_scale}")
                image_scale = 1.0 / tile_scale
                name_suffix = "" if tile_scale == 1 else f"_x{tile_scale}"
                # tile the triplets of images into NxN chips
                for sar_path, dem_path, msk_path in tqdm(list(zip(sar_files, dem_files, msk_files))):
                    image_id = Path(sar_path).stem
                    dem_tiles = _process_tiff(image_id,
                                              dem_path,
                                              subset_dir,
                                              image_type=ImageType.DEM,
                                              tiling_fn=tiler,
                                              process_fn=dem_process,
                                              scale=image_scale,
                                              resampling=Resampling.bilinear,
                                              name_suffix=name_suffix)
                    sar_tiles = _process_tiff(image_id,
                                              sar_path,
                                              subset_dir,
                                              image_type=ImageType.SAR,
                                              tiling_fn=tiler,
                                              process_fn=sar_process,
                                              scale=image_scale,
                                              resampling=Resampling.bilinear,
                                              name_suffix=name_suffix)
                    msk_tiles = _process_tiff(image_id,
                                              msk_path,
                                              subset_dir,
                                              image_type=ImageType.MASK,
                                              tiling_fn=tiler,
                                              process_fn=morph,
                                              scale=image_scale,
                                              resampling=Resampling.nearest,
                                              name_suffix=name_suffix)
                    assert (sar_tiles == dem_tiles == msk_tiles), \
                        f"Tile mismatch for {image_id}: {sar_tiles} - {dem_tiles} - {msk_tiles}"
                    # last generate context (global) images, only when the scale is 1 to avoid repeating it
                    if make_context and tile_scale == 1:
                        _process_tiff(image_id,
                                      dem_path,
                                      subset_dir,
                                      image_type=ImageType.DEM,
                                      tiling_fn=tiler,
                                      process_fn=dem_process,
                                      scale=1,
                                      resampling=Resampling.bilinear,
                                      is_context=True)
                        _process_tiff(image_id,
                                      sar_path,
                                      subset_dir,
                                      image_type=ImageType.SAR,
                                      tiling_fn=tiler,
                                      process_fn=sar_process,
                                      scale=1,
                                      resampling=Resampling.bilinear,
                                      is_context=True)
                        _process_tiff(image_id,
                                      msk_path,
                                      subset_dir,
                                      image_type=ImageType.MASK,
                                      tiling_fn=tiler,
                                      process_fn=morph,
                                      scale=1,
                                      resampling=Resampling.nearest,
                                      is_context=True)

        LOG.info("Tiling complete")
        # From here, assume tiles are done and present in dst_dir
        # continue with the preprocessing
        tile_paths = _gather_files(sar_glob=subset_dir / "sar" / "*.tif",
                                   dem_glob=subset_dir / "dem" / "*.tif",
                                   mask_glob=subset_dir / "mask" / "*.tif",
                                   check_stems=False)

        valid, removed = 0, 0
        tile_area = config.tile_size * config.tile_size

        for sar_path, dem_path, msk_path in tqdm(list(zip(*tile_paths))):
            image = imread(sar_path)
            is_context = Path(sar_path).stem.endswith("_full")
            # remove mostly nan images, using the configured percentage (excluding test images)
            # tile files are directly deleted, careful about this
            nan_mask = np.isnan(image.sum(axis=0))
            empty_pixels = np.count_nonzero(nan_mask)
            if not (is_test_set or is_context) and (empty_pixels / float(tile_area)) >= config.nan_threshold:
                _delete_group(sar_path, dem_path, msk_path)
                removed += 1
            # otherwise update nans into an actual ignore index
            # for sar and dem is not important, but the mask should be 255 for losses
            else:
                if empty_pixels > 0:
                    mask_raster(sar_path, mask=nan_mask, mask_value=ImageType.SAR.value[-1])
                    mask_raster(dem_path, mask=nan_mask, mask_value=ImageType.DEM.value[-1])
                    mask_raster(msk_path, mask=nan_mask, mask_value=ImageType.MASK.value[-1])
                valid += 1

        LOG.info(f"valid tiles: {valid}, removed tiles: {removed} ({valid / float(valid + removed) * 100.0:.2f})")
    LOG.info("Done!")


def compute_statistics(config: StatsConfig):
    # A couple prints just to be cool
    """Computes the statistics on the current dataset.
    """
    LOG.info(f"Computing dataset statistics on {config.subset} set...")
    print_config(LOG, config)

    sar_paths = sorted(list(glob(str(config.data.path / config.subset / "sar" / "*.tif"))))
    dem_paths = sorted(list(glob(str(config.data.path / config.subset / "dem" / "*.tif"))))
    msk_paths = sorted(list(glob(str(config.data.path / config.subset / "mask" / "*.tif"))))

    assert len(sar_paths) > 0 and len(sar_paths) == len(dem_paths) == len(msk_paths), "Length mismatch"

    pixel_count = 0
    ch_max = None
    ch_min = None
    ch_avg = None
    ch_std = None
    # iterate on the large tiles
    LOG.info("Computing  min, max and mean...")
    for sar_path, dem_path, mask_path in tqdm(list(zip(sar_paths, dem_paths, msk_paths))):
        image_id = Path(sar_path).stem  # .replace("sar", "")
        assert _extract_emsr(sar_path) == _extract_emsr(dem_path) == _extract_emsr(mask_path), "Image ID not matching"

        # read images
        sar = _decibel(imread(sar_path, channels_first=False))
        dem = _clip_dem(imread(dem_path, channels_first=False))
        mask = imread(mask_path, channels_first=False)
        mask = mask.reshape(_dims(mask))
        assert _dims(sar) == _dims(dem) == _dims(mask), f"Shape mismatch for {image_id}"

        # initialize vectors if it's the first iteration
        channel_count = sar.shape[-1] + dem.shape[-1]
        if ch_max is None:
            ch_max = np.ones(channel_count) * np.finfo(np.float32).min
            ch_min = np.ones(channel_count) * np.finfo(np.float32).max
            ch_avg = np.zeros(channel_count, dtype=np.float32)
            ch_std = np.zeros(channel_count, dtype=np.float32)

        valid_pixels = mask.flatten() != 255
        sar = sar.reshape((-1, sar.shape[-1]))[valid_pixels]
        dem = dem.reshape((-1, dem.shape[-1]))[valid_pixels]

        pixel_count += sar.shape[0]
        ch_max = np.maximum(ch_max, np.concatenate((sar.max(axis=0), dem.max(axis=0)), axis=-1))
        ch_min = np.minimum(ch_min, np.concatenate((sar.min(axis=0), dem.min(axis=0)), axis=-1))
        ch_avg += np.concatenate((sar.sum(axis=0), dem.sum(axis=0)), axis=-1)
    ch_avg /= float(pixel_count)

    # second pass to compute standard deviation (could be approximated in a single pass, but it's not accurate)
    LOG.info("Computing standard deviation...")
    for sar_path, dem_path in tqdm(list(zip(sar_paths, dem_paths))):
        # read images
        sar = _decibel(imread(sar_path, channels_first=False))
        dem = _clip_dem(imread(dem_path, channels_first=False))
        mask = imread(mask_path, channels_first=False)
        mask = mask.reshape(_dims(mask))
        assert _dims(sar) == _dims(dem) == _dims(mask), f"Shape mismatch for {image_id}"
        # prepare arrays by flattening everything except channels
        img_channels = sar.shape[-1]
        dem_channels = dem.shape[-1]
        valid_pixels = mask.flatten() != 255
        sar = sar.reshape((-1, img_channels))[valid_pixels]
        dem = dem.reshape((-1, dem_channels))[valid_pixels]
        # compute variance
        image_std = ((sar - ch_avg[:img_channels])**2).sum(axis=0) / float(sar.shape[0])
        dem_std = ((dem - ch_avg[img_channels:])**2).sum(axis=0) / float(sar.shape[0])
        ch_std += np.concatenate((image_std, dem_std), axis=-1)
    # square it to compute std
    ch_std = np.sqrt(ch_std / len(sar_paths))
    # print stats
    print("channel-wise max: ", ch_max)
    print("channel-wise min: ", ch_min)
    print("channel-wise avg: ", ch_avg)
    print("channel-wise std: ", ch_std)
    print("normalized avg: ", (ch_avg - ch_min) / (ch_max - ch_min))
    print("normalized std: ", ch_std / (ch_max - ch_min))


def generate_pseudolabels(config: PreparationConfig):
    LOG.info("Generating weight pseudolabels...")
    data_path = Path(config.data_processed)
    assert data_path.exists() and data_path.is_dir(), "The given path is not a valid directory"

    # this is just needed for the training set
    dataset = FloodDataset(path=data_path,
                           subset="train",
                           include_dem=False,
                           transform_base=None)
    # prepare directory to store resulting images
    result_path = data_path / "train" / "weight"
    check_or_make_dir(result_path)
    morph_kernel = MorphologyTransform().create_round_kernel(kernel_size=config.morph_kernel)

    # we could iterate the dataset normally, but we also need the filename to store results
    # if we iterate in series, it takes forever to process with NL means and morphology
    def process_image(index: int):
        image = imread(dataset.image_files[index], channels_first=False)
        label, profile = imread(dataset.label_files[index], return_metadata=True)
        label = label.squeeze(0).astype(np.uint8)
        # multiply VV and VH for fixed constants, more practical for thresholding
        image[:, :, 0] *= config.vv_multiplier
        image[:, :, 1] *= config.vh_multiplier
        # produce a smoother SAR image for a less noisy threshold
        # then further clean it up using morphological opening
        denoised = denoise_nl_means(image, h=0.1, multichannel=True)
        flooded = ((denoised[:, :, 0] <= 0.1) * (denoised[:, :, 1] <= 0.1)).astype(np.uint8)
        flooded = cv2.morphologyEx(flooded, cv2.MORPH_OPEN, morph_kernel)
        # combine them so that background has index 0, union has index 1, intersection 2
        result = flooded + label
        # store results to file
        image_name = Path(dataset.image_files[index]).name
        with rasterio.open(str(result_path / image_name), "w", **profile) as dst:
            dst.write(result[np.newaxis, ...])

    # Run a bunch of parallel jobs, using the same function
    Parallel(n_jobs=12)(delayed(process_image)(i) for i in tqdm(range(len(dataset))))
    # just some final checks, just in case
    LOG.info("Validating results...")
    result_images = glob(str(result_path / "*.tif"))
    assert len(result_images) == len(dataset), \
        f"Length mismatch between dataset ({len(dataset)}) and result ({len(result_images)})"
    LOG.info("Done!")
