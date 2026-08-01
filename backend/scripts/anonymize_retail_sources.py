"""Replace confidential source identities across persisted application data.

Mappings are supplied at runtime as base64-encoded JSON so source identities do
not need to be committed to the repository or echoed in ordinary command logs.
"""

import argparse
import base64
import json
import re
from typing import Any

from sqlalchemy import text

from app.database import SessionLocal
from app.models import Base


def replace_value(value: Any, mappings: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        result = value
        for original, replacement in mappings:
            result = re.sub(re.escape(original), replacement, result, flags=re.IGNORECASE)
        return result
    if isinstance(value, list):
        return [replace_value(item, mappings) for item in value]
    if isinstance(value, dict):
        return {
            replace_value(key, mappings): replace_value(item, mappings)
            for key, item in value.items()
        }
    return value


def decode_mappings(encoded: str) -> list[tuple[str, str]]:
    payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Mappings must be a non-empty JSON object")
    mappings = [(str(key), str(value)) for key, value in payload.items() if str(key)]
    return sorted(mappings, key=lambda item: len(item[0]), reverse=True)


def anonymize_postgres(db, mappings: list[tuple[str, str]]) -> dict[str, int]:
    """Use bounded set-based updates for large production provenance tables."""
    quote = db.bind.dialect.identifier_preparer.quote
    changed_operations: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        table_name = quote(table.name)
        table_operations = 0
        for column in table.columns:
            try:
                python_type = column.type.python_type
            except (AttributeError, NotImplementedError):
                continue
            if python_type not in {str, dict, list}:
                continue
            column_name = quote(column.name)
            for original, replacement in mappings:
                pattern = re.escape(original)
                if python_type is str:
                    statement = text(
                        f"UPDATE {table_name} SET {column_name} = "
                        f"regexp_replace({column_name}, :pattern, :replacement, 'gi') "
                        f"WHERE {column_name} ~* :pattern"
                    )
                else:
                    statement = text(
                        f"UPDATE {table_name} SET {column_name} = CAST("
                        f"regexp_replace(CAST({column_name} AS TEXT), :pattern, "
                        f":replacement, 'gi') AS JSONB) "
                        f"WHERE CAST({column_name} AS TEXT) ~* :pattern"
                    )
                result = db.execute(
                    statement,
                    {"pattern": pattern, "replacement": replacement},
                )
                table_operations += result.rowcount or 0
        if table_operations:
            changed_operations[table.name] = table_operations
    return dict(sorted(changed_operations.items()))


def anonymize(encoded_mappings: str, *, commit: bool = False) -> dict[str, int]:
    mappings = decode_mappings(encoded_mappings)
    db = SessionLocal()
    changed_rows: dict[str, int] = {}
    try:
        if db.bind.dialect.name == "postgresql":
            changed_rows = anonymize_postgres(db, mappings)
            if commit:
                db.commit()
            else:
                db.rollback()
            return changed_rows
        for mapper in Base.registry.mappers:
            model = mapper.class_
            mutable_columns = [column.key for column in mapper.columns]
            table_changes = 0
            for row in db.query(model).yield_per(100):
                changed = False
                for column_name in mutable_columns:
                    current = getattr(row, column_name)
                    if not isinstance(current, (str, list, dict)):
                        continue
                    replacement = replace_value(current, mappings)
                    if replacement != current:
                        setattr(row, column_name, replacement)
                        changed = True
                if changed:
                    table_changes += 1
            if table_changes:
                changed_rows[mapper.local_table.name] = table_changes
        if commit:
            db.commit()
        else:
            db.rollback()
        return dict(sorted(changed_rows.items()))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Anonymize confidential Retail Data source identities.")
    parser.add_argument("--mappings-base64", required=True)
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    print(json.dumps(anonymize(args.mappings_base64, commit=args.commit), indent=2))


if __name__ == "__main__":
    main()
