import pandas as pd
import os

file_path = r"c:\Users\Santosh\Documents\Google Sites Bulk Creation\gs_bulk_tool\sample 10 (1).xlsx"
if os.path.exists(file_path):
    df = pd.read_excel(file_path)
    print("Row 0 Keyword:", df.iloc[0]['Keyword'])
    print("Row 0 Title:", df.iloc[0]['Title'])
else:
    print("File not found")
