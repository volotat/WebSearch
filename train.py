"""
WebSearch/train.py — Universal evaluator training contribution.

Exposes get_training_pairs() so that universal_train.py can collect
(chunk_embeddings, user_rating) pairs from all user-rated web pages.
"""

import os
import numpy as np
from omegaconf import OmegaConf


def get_training_pairs(cfg, text_embedder, status_callback=None):
    """
    Yield (chunk_embeddings, user_rating) pairs from user-rated WebPages.

    Each page's stored .md file is read directly and embedded with
    text_embedder, making this a "full text" embedding strategy.

    Parameters
    ----------
    cfg : OmegaConf DictConfig
    text_embedder : TextEmbedder
        Shared, already-initiated text embedder.
    status_callback : callable(str) or None

    Yields
    ------
    (np.ndarray of shape [chunks, dim], float)
    """
    import modules.WebSearch.db_models as db_models

    storage_dir = OmegaConf.select(
        cfg, "WebSearch.storage_directory",
        default="/mnt/project_config/modules/WebSearch",
    )

    try:
        entries = db_models.WebPage.query.filter(
            db_models.WebPage.user_rating.isnot(None)
        ).all()
    except Exception as exc:
        print(f"[WebSearch/train] DB query failed: {exc}")
        return

    total = len(entries)
    if total == 0:
        print("[WebSearch/train] No user-rated pages found.")
        return

    print(f"[WebSearch/train] {total} user-rated pages found.")
    if status_callback:
        status_callback(f"WebSearch: found {total} user-rated pages.")

    for i, entry in enumerate(entries):
        if entry.md_file_path is None:
            continue

        full_path = os.path.join(storage_dir, entry.md_file_path)
        if not os.path.exists(full_path):
            continue

        try:
            with open(full_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content or len(content.strip()) < 10:
                continue

            chunk_embeddings = text_embedder.embed_text(content)
            if chunk_embeddings is None or len(chunk_embeddings) == 0:
                continue

            yield (np.array(chunk_embeddings, dtype=np.float32), float(entry.user_rating))

        except Exception as exc:
            print(f"[WebSearch/train] Error processing {entry.md_file_path}: {exc}")
            continue

        if status_callback and (i + 1) % 10 == 0:
            status_callback(f"WebSearch: embedded {i + 1}/{total} pages...")
