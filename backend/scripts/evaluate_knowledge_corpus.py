import argparse
import json

from app.database import SessionLocal
from app.knowledge_corpus.evaluation import evaluate_holdout, evaluate_non_ean_matching


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=100)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        print(json.dumps({
            "exact_ean": evaluate_holdout(db, args.sample_size),
            "non_ean": evaluate_non_ean_matching(db, max(200, args.sample_size)),
        }, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
