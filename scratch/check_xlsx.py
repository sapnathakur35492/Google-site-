import pandas as pd
import os

file_path = r"c:\Users\Santosh\Documents\Google Sites Bulk Creation\gs_bulk_tool\sample 10 (1).xlsx"
if os.path.exists(file_path):
    df = pd.read_excel(file_path)
    print("Columns:", df.columns.tolist())
    print("\nFirst row Title:", df.iloc[0]['Title'])
    print("\nFirst row Content Length:", len(str(df.iloc[0]['Content'])))
    print("\nFirst row Content Snippet:", str(df.iloc[0]['Content'])[:500])
else:
    print("File not found")
