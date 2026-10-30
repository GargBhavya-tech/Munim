"""Fine-tune an Indic entailment cross-encoder (Build Map #119). NOT RUN in this repository's build: it needs a GPU, hours, and the IndicXNLI data.

    python scripts/train_indic_nli.py --dry-run                       # prints the plan, downloads nothing, imports no ML library
    python scripts/train_indic_nli.py --train-file X.jsonl --eval-file Y.jsonl --download-ok   # the real run

Input files are jsonl with {"premise": ..., "hypothesis": ..., "label": "entailment" | "neutral" | "contradiction"} (hi and kn rows from IndicXNLI).
The result is written to models/indic_nli/. Its test numbers are only half the story: report it with core/eval/e5.py against OUR labelled hi and kn replies too,
because IndicXNLI's test split is machine-translated. This script has not been executed here; treat its first run as a trial.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS = ["entailment", "neutral", "contradiction"]
DEFAULT_BASE = "google/muril-base-cased"          # alternative: xlm-roberta-base


def plan(args) -> dict:
    return {"base_model": args.base, "labels": LABELS, "train_file": args.train_file, "eval_file": args.eval_file, "epochs": args.epochs,
            "batch_size": args.batch_size, "lr": args.lr, "max_length": 256, "output": str(args.out),
            "needs": ["a GPU", "transformers, datasets, torch (requirements-ml.txt)", "the IndicXNLI hi and kn files", "network access to download the base model"],
            "after": "compare with back-translation on our own labelled hi and kn replies using core/eval/e5.py"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--train-file")
    ap.add_argument("--eval-file")
    ap.add_argument("--out", type=Path, default=ROOT / "models" / "indic_nli")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-5)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--download-ok", action="store_true", help="confirm that downloading the base model and reading the data is intended")
    args = ap.parse_args(argv)
    if args.dry_run:
        print(json.dumps(plan(args), indent=1))
        return 0
    if not args.download_ok or not args.train_file or not args.eval_file:
        print("refusing to run: pass --train-file, --eval-file and --download-ok (or use --dry-run)", file=sys.stderr)
        return 2
    from datasets import load_dataset                                   # noqa: E402  imported late: nothing heavy loads for --dry-run
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, Trainer, TrainingArguments
    ds = load_dataset("json", data_files={"train": args.train_file, "eval": args.eval_file})
    tok = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForSequenceClassification.from_pretrained(args.base, num_labels=3, id2label=dict(enumerate(LABELS)), label2id={n: i for i, n in enumerate(LABELS)})

    def enc(batch):
        out = tok(batch["premise"], batch["hypothesis"], truncation=True, max_length=256)
        out["labels"] = [LABELS.index(x) for x in batch["label"]]
        return out
    ds = ds.map(enc, batched=True, remove_columns=ds["train"].column_names)
    targs = TrainingArguments(output_dir=str(args.out), num_train_epochs=args.epochs, per_device_train_batch_size=args.batch_size, learning_rate=args.lr,
                              eval_strategy="epoch", save_strategy="no", report_to=[])
    Trainer(model=model, args=targs, train_dataset=ds["train"], eval_dataset=ds["eval"], tokenizer=tok).train()
    model.save_pretrained(args.out)
    tok.save_pretrained(args.out)
    print(f"saved to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
