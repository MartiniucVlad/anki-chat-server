import pandas as pd
import random

# Load the CSV file
df = pd.read_csv('z_german4all/train.csv')

# Select a random row
random_row = df.sample(n=1).iloc[0]

# Print the text
print("Original Text:")
print(random_row['text'])
print("\nSimplifications:")

# Print all simplifications cl_1 to cl_5
for i in range(1, 6):
    cl_key = f'cl_{i}'
    print(f"CL_{i}: {random_row[cl_key]}")
    print()
