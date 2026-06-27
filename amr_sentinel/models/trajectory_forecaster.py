"""
amr_sentinel/models/trajectory_forecaster.py
=============================================
Temporal Fusion Transformer (TFT) trajectory forecaster for AMR-Sentinel.

What it does:
    Trains a TFT model on the feature-engineered resistance time-series
    dataset produced by feature_engineering.py. The model learns to predict
    resistance rates 1-3 years ahead per pathogen-antibiotic-country (PAC)
    triplet, with calibrated confidence intervals.

    After training, exposes an inference function that takes a triplet
    identifier and returns a forecast dict with point estimates and
    quantile intervals — ready for the anomaly detector in Task 2.3.

Key fixes in this version:
    1. TFTNoInterpretation subclass overrides on_validation_epoch_end
       directly — the correct entry point Lightning calls — bypassing
       the entire interpretation crash chain in pytorch_forecasting 1.7.0.
    2. train_dataset.pkl is saved alongside the model checkpoint so that
       forecast_triplet can reconstruct TimeSeriesDataSet.from_dataset()
       at inference time. The previous approach of reading
       model.hparams["dataset_parameters"] fails because pytorch_forecasting
       serialises it as a plain dict, not a TimeSeriesDataSet object.

Inputs:
    - data/processed/features_train.parquet
    - data/processed/features_val.parquet
    - data/processed/features_test.parquet
    - data/processed/feature_metadata.json

Outputs:
    - data/models/tft_amr_sentinel.ckpt       (best model checkpoint)
    - data/models/tft_amr_sentinel_final.ckpt (final epoch fallback)
    - data/models/train_dataset.pkl            (serialised training dataset
                                                for inference reconstruction)
    - data/models/training_metrics.json        (MAE, RMSE, MAPE)

External dependencies:
    pip install pytorch-forecasting lightning pyarrow
"""

import json
import logging
import os
import pickle
import warnings
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import lightning.pytorch as pl
from lightning.pytorch.callbacks import (
    EarlyStopping,
    LearningRateMonitor,
    ModelCheckpoint,
)
from lightning.pytorch.loggers import CSVLogger
from pytorch_forecasting import TemporalFusionTransformer, TimeSeriesDataSet
from pytorch_forecasting.data import GroupNormalizer
from pytorch_forecasting.metrics import QuantileLoss
from dotenv import load_dotenv

warnings.filterwarnings("ignore", ".*does not have many workers.*")
warnings.filterwarnings("ignore", ".*MPS available.*")
warnings.filterwarnings("ignore", ".*GPU available.*")
warnings.filterwarnings("ignore", ".*Attribute 'loss'.*")
warnings.filterwarnings("ignore", ".*Attribute 'logging_metrics'.*")
warnings.filterwarnings("ignore", ".*treespec.*")
warnings.filterwarnings("ignore", ".*encoder length.*")

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
PROCESSED_DIR = DATA_DIR / "processed"
MODELS_DIR = DATA_DIR / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "trajectory_forecaster.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("trajectory_forecaster")

# ---------------------------------------------------------------------------
# Hyperparameters
# ---------------------------------------------------------------------------

HIDDEN_SIZE = 32
ATTENTION_HEAD_SIZE = 2
DROPOUT = 0.15
HIDDEN_CONTINUOUS_SIZE = 16
BATCH_SIZE = 32
MAX_EPOCHS = 60
LEARNING_RATE = 3e-3
GRADIENT_CLIP_VAL = 0.15
ENCODER_LENGTH = 5
PREDICTION_LENGTH = 3
QUANTILES = [0.1, 0.25, 0.5, 0.75, 0.9]
EARLY_STOP_PATIENCE = 8
EARLY_STOP_MIN_DELTA = 1e-4
RANDOM_SEED = 42

# ---------------------------------------------------------------------------
# Feature column definitions — must match feature_engineering.py output
# ---------------------------------------------------------------------------

TIME_VARYING_KNOWN_REALS = ["year"]

TIME_VARYING_UNKNOWN_REALS = [
    "resistance_rate",
    "rate_lag1",
    "rate_lag2",
    "rate_roll3",
    "rate_roll5",
    "rate_yoy_delta",
    "rate_trend",
    "regional_mean",
    "regional_std",
    "regional_rank",
]

STATIC_CATEGORICALS = [
    "pathogen_name_code",
    "antibiotic_name_code",
    "country_iso3_code",
    "region_who_code",
    "data_source_code",
]

TARGET = "resistance_rate"
GROUP_IDS = ["pathogen_name", "antibiotic_name", "country_iso3"]


# ---------------------------------------------------------------------------
# TFT subclass — complete fix for pytorch_forecasting 1.7.0 crash chain
# ---------------------------------------------------------------------------

class TFTNoInterpretation(TemporalFusionTransformer):
    """TFT subclass that completely bypasses the interpretation crash chain.

    Root cause in pytorch_forecasting 1.7.0:
        Lightning calls on_validation_epoch_end (base._base_model)
        -> self.on_epoch_end(outputs)
        -> self.log_interpretation(outputs)
        -> self.interpret_output(...)
        -> integer_histogram(...) RuntimeError: index out of bounds
        Even if _log_interpretation returns {}, log_interpretation then
        accesses interpretation["encoder_length_histogram"] -> KeyError

    Fix: override on_validation_epoch_end directly. This is the entry
    point Lightning calls. Our override logs val_loss (required by
    ModelCheckpoint and EarlyStopping) then returns — the crash chain
    is never entered. All other overrides are belt-and-braces.
    """

    def on_validation_epoch_end(self) -> None:
        """Override: log val_loss, skip interpretation entirely."""
        outputs = self.validation_step_outputs
        if not outputs:
            return
        try:
            import torch
            losses = [
                o["loss"] for o in outputs
                if isinstance(o, dict) and "loss" in o
            ]
            if losses:
                mean_loss = torch.stack(losses).mean()
                self.log(
                    "val_loss",
                    mean_loss,
                    on_epoch=True,
                    prog_bar=True,
                    batch_size=BATCH_SIZE,
                )
        except Exception as exc:
            logger.debug("Could not compute val_loss: %s", exc)
        self.validation_step_outputs.clear()

    def on_epoch_end(self, outputs: list) -> None:
        """Override: no-op. Prevents log_interpretation from being called."""
        return None

    def log_interpretation(self, outputs: list) -> None:
        """Override: no-op. Prevents interpret_output from being called."""
        return None

    def _log_interpretation(self, out: dict) -> dict:
        """Override: returns empty dict instead of crashing."""
        return {}


# ---------------------------------------------------------------------------
# Data loading and preparation
# ---------------------------------------------------------------------------

def load_processed_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    """Load processed Parquet files and feature metadata.

    Returns:
        Tuple of (train_df, val_df, test_df, metadata_dict)

    Raises:
        FileNotFoundError: If processed files do not exist.
    """
    train_path = PROCESSED_DIR / "features_train.parquet"
    val_path = PROCESSED_DIR / "features_val.parquet"
    test_path = PROCESSED_DIR / "features_test.parquet"
    meta_path = PROCESSED_DIR / "feature_metadata.json"

    for p in [train_path, val_path, test_path, meta_path]:
        if not p.exists():
            raise FileNotFoundError(
                f"Required file not found: {p}. "
                "Run feature_engineering.py first."
            )

    train_df = pd.read_parquet(train_path)
    val_df = pd.read_parquet(val_path)
    test_df = pd.read_parquet(test_path)

    with open(meta_path, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    logger.info(
        "Loaded: train=%d, val=%d, test=%d rows",
        len(train_df), len(val_df), len(test_df),
    )
    return train_df, val_df, test_df, metadata


def prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare a DataFrame for TimeSeriesDataSet consumption.

    Adds time_idx, converts types, forward-fills remaining NaN values.

    Args:
        df: Feature-engineered DataFrame (train, val, or test).

    Returns:
        Prepared DataFrame ready for TimeSeriesDataSet.
    """
    df = df.copy()

    for col in GROUP_IDS:
        df[col] = df[col].astype(str)

    for col in STATIC_CATEGORICALS:
        if col in df.columns:
            df[col] = df[col].fillna(0).astype(int).astype(str)

    df = df.sort_values(GROUP_IDS + ["year"]).reset_index(drop=True)
    df["time_idx"] = df.groupby(GROUP_IDS)["year"].transform(
        lambda s: (s - s.min()).astype(int)
    )

    continuous_cols = TIME_VARYING_UNKNOWN_REALS + TIME_VARYING_KNOWN_REALS
    for col in continuous_cols:
        if col in df.columns:
            df[col] = df.groupby(GROUP_IDS)[col].transform(
                lambda s: s.ffill().bfill()
            )
            if df[col].isna().any():
                logger.warning(
                    "Column %s: zeroing %d remaining NaN.",
                    col, df[col].isna().sum(),
                )
                df[col] = df[col].fillna(0.0)

    return df


def build_datasets(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> tuple[TimeSeriesDataSet, TimeSeriesDataSet, TimeSeriesDataSet]:
    """Build PyTorch Forecasting TimeSeriesDataSet objects.

    Args:
        train_df, val_df, test_df: Prepared DataFrames.

    Returns:
        Tuple of (train_dataset, val_dataset, test_dataset).
    """
    train_val_df = pd.concat([train_df, val_df], ignore_index=True)
    max_train_val_idx = int(train_val_df["time_idx"].max())

    train_dataset = TimeSeriesDataSet(
        train_val_df[
            train_val_df["time_idx"] <= max_train_val_idx - PREDICTION_LENGTH
        ],
        time_idx="time_idx",
        target=TARGET,
        group_ids=GROUP_IDS,
        min_encoder_length=max(1, ENCODER_LENGTH // 2),
        max_encoder_length=ENCODER_LENGTH,
        min_prediction_length=1,
        max_prediction_length=PREDICTION_LENGTH,
        static_categoricals=STATIC_CATEGORICALS,
        static_reals=[],
        time_varying_known_reals=TIME_VARYING_KNOWN_REALS,
        time_varying_unknown_reals=TIME_VARYING_UNKNOWN_REALS,
        target_normalizer=GroupNormalizer(
            groups=GROUP_IDS,
            transformation="softplus",
        ),
        add_relative_time_idx=True,
        add_target_scales=True,
        add_encoder_length=True,
        allow_missing_timesteps=True,
    )

    val_dataset = TimeSeriesDataSet.from_dataset(
        train_dataset, train_val_df, predict=True, stop_randomization=True,
    )

    full_df = pd.concat([train_val_df, test_df], ignore_index=True)
    test_dataset = TimeSeriesDataSet.from_dataset(
        train_dataset, full_df, predict=True, stop_randomization=True,
    )

    logger.info(
        "Datasets built. Train: %d samples | Val: %d samples | Test: %d samples",
        len(train_dataset), len(val_dataset), len(test_dataset),
    )
    return train_dataset, val_dataset, test_dataset


def build_dataloaders(
    train_dataset: TimeSeriesDataSet,
    val_dataset: TimeSeriesDataSet,
    test_dataset: TimeSeriesDataSet,
) -> tuple:
    """Build DataLoaders. Uses num_workers=0 for Windows compatibility.

    Args:
        train_dataset, val_dataset, test_dataset: Built datasets.

    Returns:
        Tuple of (train_loader, val_loader, test_loader).
    """
    train_loader = train_dataset.to_dataloader(
        train=True, batch_size=BATCH_SIZE, num_workers=0, shuffle=True,
    )
    val_loader = val_dataset.to_dataloader(
        train=False, batch_size=BATCH_SIZE * 2, num_workers=0, shuffle=False,
    )
    test_loader = test_dataset.to_dataloader(
        train=False, batch_size=BATCH_SIZE * 2, num_workers=0, shuffle=False,
    )
    return train_loader, val_loader, test_loader


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------

def build_tft_model(train_dataset: TimeSeriesDataSet) -> TFTNoInterpretation:
    """Construct TFTNoInterpretation from the training dataset schema.

    Args:
        train_dataset: Built TimeSeriesDataSet defining the schema.

    Returns:
        Configured TFTNoInterpretation ready for training.
    """
    pl.seed_everything(RANDOM_SEED)

    model = TFTNoInterpretation.from_dataset(
        train_dataset,
        learning_rate=LEARNING_RATE,
        hidden_size=HIDDEN_SIZE,
        attention_head_size=ATTENTION_HEAD_SIZE,
        dropout=DROPOUT,
        hidden_continuous_size=HIDDEN_CONTINUOUS_SIZE,
        loss=QuantileLoss(quantiles=QUANTILES),
        optimizer="adam",
        log_interval=10,
        log_val_interval=1,
        reduce_on_plateau_patience=4,
    )

    logger.info(
        "TFT model built. Parameters: %s",
        f"{sum(p.numel() for p in model.parameters()):,}",
    )
    return model


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_model(
    model: TFTNoInterpretation,
    train_loader,
    val_loader,
    train_dataset: TimeSeriesDataSet,
) -> tuple[TFTNoInterpretation, pl.Trainer, str]:
    """Train the TFT model with early stopping and checkpointing.

    Saves train_dataset.pkl alongside the checkpoint so that
    forecast_triplet can reconstruct TimeSeriesDataSet.from_dataset()
    at inference time without needing to reload all training data.

    Args:
        model: Configured TFTNoInterpretation model.
        train_loader: Training DataLoader.
        val_loader: Validation DataLoader.
        train_dataset: The built TimeSeriesDataSet (saved to disk for
                       inference reconstruction).

    Returns:
        Tuple of (trained_model, trainer, best_checkpoint_path).
    """
    checkpoint_callback = ModelCheckpoint(
        dirpath=str(MODELS_DIR),
        filename="tft_amr_sentinel",
        monitor="val_loss",
        mode="min",
        save_top_k=1,
        verbose=True,
    )

    early_stop_callback = EarlyStopping(
        monitor="val_loss",
        min_delta=EARLY_STOP_MIN_DELTA,
        patience=EARLY_STOP_PATIENCE,
        verbose=True,
        mode="min",
    )

    lr_monitor = LearningRateMonitor(logging_interval="epoch")
    csv_logger = CSVLogger(save_dir=str(MODELS_DIR), name="training_logs")

    trainer = pl.Trainer(
        max_epochs=MAX_EPOCHS,
        accelerator="cpu",
        devices=1,
        gradient_clip_val=GRADIENT_CLIP_VAL,
        callbacks=[checkpoint_callback, early_stop_callback, lr_monitor],
        logger=csv_logger,
        enable_progress_bar=True,
        log_every_n_steps=5,
        deterministic=True,
    )

    logger.info("Starting TFT training on CPU...")
    logger.info(
        "Config: max_epochs=%d, batch_size=%d, lr=%.4f, hidden=%d",
        MAX_EPOCHS, BATCH_SIZE, LEARNING_RATE, HIDDEN_SIZE,
    )

    trainer.fit(model, train_dataloaders=train_loader, val_dataloaders=val_loader)

    # Save training dataset for inference reconstruction.
    # model.hparams["dataset_parameters"] is serialised as a plain dict
    # by pytorch_forecasting, not as a TimeSeriesDataSet object, so
    # TimeSeriesDataSet.from_dataset() cannot use it at inference time.
    # Pickling the actual dataset object solves this cleanly.
    dataset_pkl_path = MODELS_DIR / "train_dataset.pkl"
    with open(dataset_pkl_path, "wb") as f:
        pickle.dump(train_dataset, f)
    logger.info("Saved training dataset to %s", dataset_pkl_path)

    best_checkpoint = checkpoint_callback.best_model_path
    logger.info("Training complete. Best checkpoint: %s", best_checkpoint)

    return model, trainer, best_checkpoint


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_model(
    model: TFTNoInterpretation,
    val_loader,
    test_loader,
    trainer: pl.Trainer,
) -> dict[str, float]:
    """Evaluate trained model on validation and test sets.

    Computes MAE, RMSE, and MAPE for the median forecast (q50).

    Args:
        model: Trained TFTNoInterpretation model.
        val_loader: Validation DataLoader.
        test_loader: Test DataLoader.
        trainer: Lightning Trainer (for API consistency).

    Returns:
        Dict with val_mae, val_rmse, val_mape, test_mae, test_rmse, test_mape.
    """
    metrics: dict[str, float] = {}

    for split_name, loader in [("val", val_loader), ("test", test_loader)]:
        try:
            predictions = model.predict(
                loader,
                mode="prediction",
                return_y=True,
                trainer_kwargs={"accelerator": "cpu"},
            )

            preds_raw = predictions.output
            targets_raw = predictions.y[0]

            if preds_raw.ndim == 3:
                median_idx = len(QUANTILES) // 2
                preds = preds_raw[:, :, median_idx].numpy().flatten()
            else:
                preds = preds_raw.numpy().flatten()

            targets = targets_raw.numpy().flatten()
            valid_mask = np.isfinite(preds) & np.isfinite(targets)
            preds = preds[valid_mask]
            targets = targets[valid_mask]

            if len(preds) == 0:
                logger.warning("No valid predictions for %s split.", split_name)
                continue

            mae = float(np.mean(np.abs(preds - targets)))
            rmse = float(np.sqrt(np.mean((preds - targets) ** 2)))
            nonzero = targets != 0
            mape = float(
                np.mean(np.abs(
                    (preds[nonzero] - targets[nonzero]) / targets[nonzero]
                )) * 100
            ) if nonzero.any() else float("nan")

            metrics[f"{split_name}_mae"] = round(mae, 5)
            metrics[f"{split_name}_rmse"] = round(rmse, 5)
            metrics[f"{split_name}_mape"] = round(mape, 3)

            logger.info(
                "%s — MAE: %.4f | RMSE: %.4f | MAPE: %.2f%%",
                split_name.upper(), mae, rmse, mape,
            )

        except Exception as exc:
            logger.error("Evaluation failed for %s split: %s", split_name, exc)

    return metrics


# ---------------------------------------------------------------------------
# Save artifacts
# ---------------------------------------------------------------------------

def save_training_artifacts(
    metrics: dict[str, float],
    best_checkpoint: str,
) -> None:
    """Save training metrics and hyperparameters to JSON.

    Args:
        metrics: Evaluation metrics dict.
        best_checkpoint: Path to the best model checkpoint file.
    """
    artifact = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "best_checkpoint": best_checkpoint,
        "train_dataset_pkl": str(MODELS_DIR / "train_dataset.pkl"),
        "metrics": metrics,
        "hyperparameters": {
            "hidden_size": HIDDEN_SIZE,
            "attention_head_size": ATTENTION_HEAD_SIZE,
            "dropout": DROPOUT,
            "hidden_continuous_size": HIDDEN_CONTINUOUS_SIZE,
            "batch_size": BATCH_SIZE,
            "max_epochs": MAX_EPOCHS,
            "learning_rate": LEARNING_RATE,
            "gradient_clip_val": GRADIENT_CLIP_VAL,
            "encoder_length": ENCODER_LENGTH,
            "prediction_length": PREDICTION_LENGTH,
            "quantiles": QUANTILES,
            "random_seed": RANDOM_SEED,
        },
    }
    metrics_path = MODELS_DIR / "training_metrics.json"
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, indent=2)
    logger.info("Saved training metrics to %s", metrics_path)


# ---------------------------------------------------------------------------
# Inference API
# ---------------------------------------------------------------------------

def load_model_for_inference(
    checkpoint_path: Optional[str] = None,
) -> TFTNoInterpretation:
    """Load the trained TFT model for inference.

    Args:
        checkpoint_path: Optional path to .ckpt file.
                         Defaults to data/models/tft_amr_sentinel.ckpt.

    Returns:
        Loaded TFTNoInterpretation in eval mode.

    Raises:
        FileNotFoundError: If no checkpoint exists.
    """
    if checkpoint_path is None:
        default_path = MODELS_DIR / "tft_amr_sentinel.ckpt"
        if not default_path.exists():
            raise FileNotFoundError(
                f"No checkpoint at {default_path}. Run run_training() first."
            )
        checkpoint_path = str(default_path)

    model = TFTNoInterpretation.load_from_checkpoint(checkpoint_path)
    model.eval()
    logger.info("Loaded model from: %s", checkpoint_path)
    return model


def load_reference_dataset(
    dataset_pkl_path: Optional[Path] = None,
) -> TimeSeriesDataSet:
    """Load the pickled training dataset for inference reconstruction.

    The training dataset is saved as train_dataset.pkl during training.
    It is needed by TimeSeriesDataSet.from_dataset() to ensure consistent
    normalisation and encoding at inference time.

    Args:
        dataset_pkl_path: Optional explicit path to the pickle file.

    Returns:
        Loaded TimeSeriesDataSet object.

    Raises:
        FileNotFoundError: If the pickle file does not exist.
    """
    if dataset_pkl_path is None:
        dataset_pkl_path = MODELS_DIR / "train_dataset.pkl"

    if not dataset_pkl_path.exists():
        raise FileNotFoundError(
            f"Training dataset pickle not found at {dataset_pkl_path}. "
            "Re-run training to regenerate it."
        )

    with open(dataset_pkl_path, "rb") as f:
        dataset = pickle.load(f)

    logger.debug("Loaded reference dataset from %s", dataset_pkl_path)
    return dataset


def forecast_triplet(
    model: TFTNoInterpretation,
    train_df: pd.DataFrame,
    pathogen: str,
    antibiotic: str,
    country_iso3: str,
    n_years_ahead: int = 3,
    reference_dataset: Optional[TimeSeriesDataSet] = None,
) -> Optional[dict]:
    """Generate a resistance rate forecast for a single PAC triplet.

    Uses the most recent ENCODER_LENGTH years of observed data for the
    triplet as the encoder input, and returns forecasts for the next
    n_years_ahead years with quantile confidence intervals.

    The reference_dataset parameter accepts a pre-loaded TimeSeriesDataSet
    to avoid reloading train_dataset.pkl on every call when forecasting
    multiple triplets in a loop (pass the same object each time).

    Args:
        model: Loaded TFTNoInterpretation in eval mode.
        train_df: Full prepared training+val DataFrame (encoder context).
        pathogen: e.g. "Escherichia coli"
        antibiotic: e.g. "Ciprofloxacin"
        country_iso3: e.g. "NGA"
        n_years_ahead: Years ahead to forecast (max PREDICTION_LENGTH).
        reference_dataset: Optional pre-loaded training TimeSeriesDataSet.
                           If None, loads from train_dataset.pkl automatically.

    Returns:
        Dict with triplet, last_observed_year, last_observed_rate,
        forecasts list [{year, median, lower_80, upper_80, lower_50, upper_50}]
        or None if the triplet is not found or inference fails.
    """
    n_years_ahead = min(n_years_ahead, PREDICTION_LENGTH)

    triplet_df = train_df[
        (train_df["pathogen_name"] == pathogen)
        & (train_df["antibiotic_name"] == antibiotic)
        & (train_df["country_iso3"] == country_iso3)
    ].copy()

    if len(triplet_df) < max(1, ENCODER_LENGTH // 2):
        logger.warning(
            "Triplet %s/%s/%s has only %d rows — insufficient.",
            pathogen, antibiotic, country_iso3, len(triplet_df),
        )
        return None

    triplet_df = triplet_df.sort_values("year").tail(ENCODER_LENGTH)
    last_year = int(triplet_df["year"].max())
    last_rate = float(triplet_df["resistance_rate"].iloc[-1])

    # Build minimal forecast DataFrame with PREDICTION_LENGTH future rows
    future_rows = []
    for i in range(1, PREDICTION_LENGTH + 1):
        row = triplet_df.iloc[-1].to_dict()
        row["year"] = last_year + i
        row["resistance_rate"] = last_rate  # placeholder
        future_rows.append(row)

    forecast_df = pd.concat(
        [triplet_df, pd.DataFrame(future_rows)], ignore_index=True
    )
    forecast_df["time_idx"] = range(len(forecast_df))

    try:
        # Load reference dataset if not provided.
        # This is the fix for the 'dict has no attribute get_parameters' error:
        # pytorch_forecasting serialises dataset_parameters as a plain dict
        # in hparams, so we cannot use model.hparams["dataset_parameters"]
        # with TimeSeriesDataSet.from_dataset(). Instead we load the actual
        # TimeSeriesDataSet object pickled during training.
        if reference_dataset is None:
            reference_dataset = load_reference_dataset()

        inference_dataset = TimeSeriesDataSet.from_dataset(
            reference_dataset,
            forecast_df,
            predict=True,
            stop_randomization=True,
        )
        inference_loader = inference_dataset.to_dataloader(
            train=False, batch_size=1, num_workers=0
        )
        raw_preds = model.predict(
            inference_loader,
            mode="quantiles",
            return_y=False,
            trainer_kwargs={"accelerator": "cpu"},
        )
    except Exception as exc:
        logger.error(
            "Inference failed for %s/%s/%s: %s",
            pathogen, antibiotic, country_iso3, exc,
        )
        return None

    # raw_preds shape: (1, prediction_length, n_quantiles)
    preds = raw_preds[0].numpy()

    forecasts = []
    for i in range(n_years_ahead):
        q = preds[i]  # [q10, q25, q50, q75, q90]
        forecasts.append({
            "year": last_year + i + 1,
            "median": round(float(np.clip(q[2], 0.0, 1.0)), 4),
            "lower_80": round(float(np.clip(q[0], 0.0, 1.0)), 4),
            "upper_80": round(float(np.clip(q[4], 0.0, 1.0)), 4),
            "lower_50": round(float(np.clip(q[1], 0.0, 1.0)), 4),
            "upper_50": round(float(np.clip(q[3], 0.0, 1.0)), 4),
        })

    return {
        "triplet": (pathogen, antibiotic, country_iso3),
        "last_observed_year": last_year,
        "last_observed_rate": round(last_rate, 4),
        "forecasts": forecasts,
    }


# ---------------------------------------------------------------------------
# Full training pipeline
# ---------------------------------------------------------------------------

def run_training() -> dict:
    """Run the complete TFT training pipeline end to end.

    Returns:
        Dict with training metrics and checkpoint path.
    """
    started_at = datetime.now(timezone.utc)
    logger.info("=" * 60)
    logger.info("TFT training pipeline started: %s", started_at.isoformat())

    train_df_raw, val_df_raw, test_df_raw, metadata = load_processed_data()

    train_df = prepare_dataframe(train_df_raw)
    val_df = prepare_dataframe(val_df_raw)
    test_df = prepare_dataframe(test_df_raw)

    train_dataset, val_dataset, test_dataset = build_datasets(
        train_df, val_df, test_df
    )
    train_loader, val_loader, test_loader = build_dataloaders(
        train_dataset, val_dataset, test_dataset
    )

    model = build_tft_model(train_dataset)

    # Pass train_dataset so train_model can pickle it for inference
    trained_model, trainer, best_checkpoint = train_model(
        model, train_loader, val_loader, train_dataset
    )

    if best_checkpoint and Path(best_checkpoint).exists():
        best_model = TFTNoInterpretation.load_from_checkpoint(best_checkpoint)
    else:
        best_model = trained_model
        best_checkpoint = str(MODELS_DIR / "tft_amr_sentinel_final.ckpt")
        trainer.save_checkpoint(best_checkpoint)

    metrics = evaluate_model(best_model, val_loader, test_loader, trainer)
    save_training_artifacts(metrics, best_checkpoint)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info("-" * 60)
    logger.info("Training complete in %.1fs (%.1f min)", elapsed, elapsed / 60)
    logger.info("Best checkpoint: %s", best_checkpoint)
    for k, v in metrics.items():
        logger.info("  %s: %s", k, v)
    logger.info("=" * 60)

    return {"metrics": metrics, "checkpoint": best_checkpoint}


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    result = run_training()

    print("\n" + "=" * 50)
    print("TFT Training Complete")
    print("=" * 50)
    print(f"Checkpoint     : {result['checkpoint']}")
    print(f"Dataset pickle : {MODELS_DIR / 'train_dataset.pkl'}")
    print("\nMetrics:")
    for k, v in result["metrics"].items():
        print(f"  {k:20s}: {v}")
    print("=" * 50)
    sys.exit(0)