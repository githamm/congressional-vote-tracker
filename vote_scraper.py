#!/usr/bin/python3
import requests
from bs4 import BeautifulSoup4
import pandas as pd
import time
import os

def scrape_votes(member_id, max_pages=None):
    """
    Scrape voting data for a specific House member from the clerk.house.gov website.
    
    Args:
        member_id (str): The member ID (e.g., 'B000825')
        max_pages (int, optional): Maximum number of pages to scrape. If None, scrape all pages.
    
    Returns:
        pandas.DataFrame: DataFrame containing all voting data
    """
    base_url = "https://clerk.house.gov/Members/ViewRecentVotes"
    all_votes = []
    page = 1
    
    while True:
        # Construct URL for current page
        url = f"{base_url}?memberID={member_id}&page={page}"
        print(f"Scraping page {page}...")
        
        # Send request with proper headers
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.110 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://clerk.house.gov/'
        }
        
        response = requests.get(url, headers=headers)
        
        # Check if request was successful
        if response.status_code != 200:
            print(f"Failed to retrieve page {page}: Status code {response.status_code}")
            break
        
        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find the table with voting data
        table = soup.find('table', class_='table')
        td = soup.find('td')
        
        if not table:
            print("No table found on the page. Stopping.")
            break

        if td.string == 'No votes found':
            print("No more votes. Stopping.")
            break   
        
        # Extract rows from the table
        rows = table.find_all('tr')
        
        # Skip header row
        rows = rows[1:]
        
        # If no data rows found, we've reached the end
        if not rows:
            #print("No more data rows found. Stopping.")
            break
        
        # Extract data from each row
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 5:  # Ensure we have enough columns
                vote_data = {
                    'Date': cols[0].text.strip(),
                    'Roll Call Number': cols[1].text.strip(),
                    'Bill Number': cols[2].text.strip(),
                    'Bill Title': cols[3].text.strip(),
                    member_id: cols[4].text.strip(),
                    'Status': cols[5].text.strip()
                }
                all_votes.append(vote_data)
        
        # Check if we've reached the maximum number of pages to scrape
        if max_pages and page >= max_pages:
            #print(f"Reached maximum number of pages ({max_pages}). Stopping.")
            break
        
        # Move to the next page
        page += 1
        
        # Add a small delay to avoid overloading the server
        time.sleep(1)
    
    # Convert to DataFrame
    df = pd.DataFrame(all_votes)
    print(f"Scraped a total of {len(df)} votes across {page} pages.")
    return df

def main(member_id):
    # Scrape all pages (or set max_pages to limit)
    votes_df = scrape_votes(member_id, 1000)
    
    # Save to CSV
    output_file = f"representative_{member_id}_votes.csv"
    votes_df.to_csv(output_file, index=False)
    print(f"Data saved to {output_file}")
    
    # Display sample of data
    # print("\nSample of scraped data:")
    # print(votes_df.head())
main("B000825")
main("C001121")
main("C001137")
main("D000197")
main("E000300")
main("H001100")
main("N000191")
main("P000620")
# main("C001121", crow_dataframe, crow_votes)
# all_data = boebert_dataframe.join(crow_dataframe)

def load_data(filename):
    return pd.read_csv(filename)

all_votes_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_B000825_votes.csv") #This is Boebert's dataframe
crank_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_C001137_votes.csv")
crow_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_C001121_votes.csv")
degette_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_D000197_votes.csv")
evans_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_E000300_votes.csv")
hurd_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_H001100_votes.csv")
neguse_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_N000191_votes.csv")
pettersen_df = load_data("/Volumes/Lacie Usable/repos/web-scraper/representative_P000620_votes.csv")

all_votes_df['C001137'] = all_votes_df['Roll Call Number'].map(crank_df.set_index('Roll Call Number')['C001137'])
all_votes_df['C001121'] = all_votes_df['Roll Call Number'].map(crow_df.set_index('Roll Call Number')['C001121'])
all_votes_df['D000197'] = all_votes_df['Roll Call Number'].map(degette_df.set_index('Roll Call Number')['D000197'])
all_votes_df['E000300'] = all_votes_df['Roll Call Number'].map(evans_df.set_index('Roll Call Number')['E000300'])
all_votes_df['H001100'] = all_votes_df['Roll Call Number'].map(hurd_df.set_index('Roll Call Number')['H001100'])
all_votes_df['N000191'] = all_votes_df['Roll Call Number'].map(neguse_df.set_index('Roll Call Number')['N000191'])
all_votes_df['P000620'] = all_votes_df['Roll Call Number'].map(pettersen_df.set_index('Roll Call Number')['P000620'])

print (all_votes_df)
output_file = f"all_votes.csv"
all_votes_df.to_csv(output_file, index=False)
print(f"Data saved to {output_file}")
