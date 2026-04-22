import asyncio
import os
from database_clients.database_mongo import connect_to_mongo, close_mongo_connection, get_db

# --- CONFIG ---
PAGE_SIZE = 5


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


def print_entry(entry, index=None):
    """
    Pretty prints a single dictionary card.
    """
    word = entry.get('word', 'Unknown')
    pos = entry.get('pos', 'Unknown')
    gender = entry.get('gender', '')
    plurals = entry.get('plurals', [])
    definitions = entry.get('definitions', [])

    # Visual formatting
    header = f" {word} "
    if gender:
        header += f"({gender}) "
    header += f"[{pos}]"

    if index is not None:
        print(f"#{index} {header}")
    else:
        print(f"\n{'=' * 40}")
        print(header.center(40))
        print(f"{'-' * 40}")

    # Print Definitions
    print("  Definitions:")
    if not definitions:
        print("    (No definitions found)")
    else:
        for i, d in enumerate(definitions, 1):
            # Clean up the definition text if it's messy
            clean_def = d.replace("\n", " ").strip()
            print(f"    {i}. {clean_def}")

    # Print Grammar Metadata
    if plurals:
        print(f"\n  Plural: {', '.join(plurals)}")

    print(f"{'=' * 40}\n")


async def browse_mode(collection):
    """
    Allows the user to page through the dictionary 5 words at a time.
    """
    skip = 0
    while True:
        clear_screen()
        print(f"--- BROWSE MODE (Skipping {skip}) ---")

        # Fetch a page of words
        cursor = collection.find({}).skip(skip).limit(PAGE_SIZE)
        results = await cursor.to_list(length=PAGE_SIZE)

        if not results:
            print("End of database reached.")
            input("Press Enter to return...")
            break

        for i, entry in enumerate(results):
            print_entry(entry, index=skip + i + 1)

        print(f"\n[n] Next Page  |  [p] Previous Page  |  [q] Quit to Menu")
        choice = input("Action: ").strip().lower()

        if choice == 'n':
            skip += PAGE_SIZE
        elif choice == 'p':
            skip = max(0, skip - PAGE_SIZE)
        elif choice == 'q':
            break


async def search_mode(collection):
    while True:
        clear_screen()
        print("--- SMART SEARCH MODE ---")
        query = input("Enter word (or 'q' to quit): ").strip()

        if query.lower() == 'q':
            break

        # 1. Broad search using regex (starts with, case-insensitive)
        db_query = {"word": {"$regex": f"^{query}", "$options": "i"}}

        # 2. Fetch more results (10 instead of 3) so we can sort them in Python
        cursor = collection.find(db_query).limit(20)
        results = await cursor.to_list(length=20)

        if not results:
            print(f"\nNo results found for '{query}'")
            input("Press Enter to continue...")
        else:
            # 3. SENIOR LOGIC: Sort the results in Python for better UX
            # Priority 1: Exact case match (lowercase verb matches lowercase query)
            # Priority 2: Shorter words first (removes the 'metropole' noise)
            results.sort(key=lambda x: (x['word'] != query, len(x['word'])))

            print(f"\nFound {len(results)} matches (Showing top 5):")
            for entry in results[:5]:  # Show top 5 most relevant
                print_entry(entry)

            input("Press Enter to search again...")

async def main():
    # 1. Connect
    await connect_to_mongo()
    db = get_db()

    if db is None:
        print("Could not connect to database.")
        return

    collection = db["dictionary"]

    # 2. Main Menu Loop
    while True:
        clear_screen()
        # Get count for cool stats
        count = await collection.count_documents({})

        print(f"GOETHE-NEURAL DICTIONARY VIEWER")
        print(f"Total Words in DB: {count}")
        print("-" * 30)
        print("1. Browse (Pagination)")
        print("2. Search Word")
        print("3. Exit")
        print("-" * 30)

        choice = input("Select option: ").strip()

        if choice == '1':
            await browse_mode(collection)
        elif choice == '2':
            await search_mode(collection)
        elif choice == '3':
            print("Goodbye.")
            break

    # 3. Cleanup
    await close_mongo_connection()


if __name__ == "__main__":
    # Windows fix for asyncio loop
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())