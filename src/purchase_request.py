"""Typed purchase-request loading, independent of supplier extraction."""
from pathlib import Path
from decimal import Decimal, InvalidOperation
import pandas as pd

SAMPLE_DIR = Path(__file__).resolve().parents[1] / 'sample-data' / 'task1-procurement'
PR_COLUMNS = ['PR number', 'Item', 'Description', 'Unit', 'Quantity requested', 'Maximum unit price (BDT)']

def read_purchase_request(path: str | Path = SAMPLE_DIR / 'purchase-request.csv') -> pd.DataFrame:
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, ValueError) as exc:
        raise ValueError(f'Cannot read purchase request: {exc}') from exc
    if list(frame.columns) != PR_COLUMNS:
        raise ValueError(f'Purchase request must contain columns in order: {PR_COLUMNS}')
    if frame.empty or frame.apply(lambda col: col.str.strip().eq('')).any().any():
        raise ValueError('Purchase request contains empty rows or values.')
    if frame.duplicated(['PR number', 'Item']).any():
        raise ValueError('Purchase request contains duplicate item keys.')
    for column in PR_COLUMNS[-2:]:
        try:
            values = frame[column].map(Decimal)
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f'Invalid numeric values in {column}') from exc
        if not all(value.is_finite() and value > 0 for value in values):
            raise ValueError(f'{column} must contain finite positive numbers.')
        frame[column] = values
    frame.attrs['source_filename'] = Path(path).name
    return frame


def purchase_request_items(frame: pd.DataFrame):
    """Build typed rows without passing money or quantities through float."""
    from src.models import PurchaseRequestItem
    return [PurchaseRequestItem(
        pr_number=row['PR number'], item_code=row['Item'],
        description=row['Description'], unit=row['Unit'],
        quantity=row['Quantity requested'], maximum_unit_price=row['Maximum unit price (BDT)'],
        source_filename=str(frame.attrs.get('source_filename', 'purchase-request.csv')),
    ) for row in frame.to_dict('records')]

