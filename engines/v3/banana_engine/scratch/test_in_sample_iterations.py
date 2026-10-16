import pandas as pd
import numpy as np

df = pd.read_parquet('QuantSync_Lines/banana_engine/data/ml_engine_v2_candidates.parquet')
train = df[df['year'] <= 2024].copy()
print(f"Total 2020-2024 candidate setups: {len(train)}")

# Let's explore what filters & exit rules produce:
# 1. Trade count: ~1000 - 1250 over 5 years (~200-250/yr)
# 2. Win rate: >= 50%
# 3. Profit factor: >= 2.0

# In train, what are the distributions of mfe_r and pnl under various exit targets?
# For example, if target is 2.0R, 2.5R, or 1.5R with trailing/breakeven:
for target_r in [1.5, 2.0, 2.5, 3.0]:
    # Suppose a trade exits at target_r if mfe_r >= target_r before stop out (-1.0R)
    # Note: label is reached 2R before stop.
    wins = (train['label'] == 1) if target_r == 2.0 else (train['mfe_r'] >= target_r)
    # But note: does mfe_r reach target_r before or after mae_r hits -1.0R?
    # In build_ml_v2_dataset, if lj <= stop_p, it broke out of loop!
    # So mfe_r was recorded UP TO the stop bar!
    # Let's check this: if mfe_r >= target_r, did it reach it before stop?
    pass

print(train[['mfe_r', 'mae_r', 'label', 'pnl_r']].head())
