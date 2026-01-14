#!/usr/bin/python
import requests
from bs4 import BeautifulSoup
import pandas as pd
import time
import os
from datetime import datetime

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
        print(f"Scraping page {page} for {member_id}...")
        
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

        if td and td.string == 'No votes found':
            print("No more votes. Stopping.")
            break   
        
        # Extract rows from the table
        rows = table.find_all('tr')
        
        # Skip header row
        rows = rows[1:]
        
        # If no data rows found, we've reached the end
        if not rows:
            break
        
        # Extract data from each row
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 5:  # Ensure we have enough columns
                date_str = cols[0].text.strip()
                roll_call = cols[1].text.strip()
                
                # Extract year from date and prepend to roll call number
                try:
                    # Try parsing common date formats
                    for fmt in ['%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d']:
                        try:
                            date_obj = datetime.strptime(date_str, fmt)
                            year = str(date_obj.year)
                            roll_call_with_year = f"{year}{roll_call}"
                            break
                        except ValueError:
                            continue
                    else:
                        # If no format matches, keep original
                        roll_call_with_year = roll_call
                except:
                    roll_call_with_year = roll_call
                
                vote_data = {
                    'Date': date_str,
                    'Roll Call Number': roll_call_with_year,
                    'Bill Number': cols[2].text.strip(),
                    'Bill Title': cols[3].text.strip(),
                    member_id: cols[4].text.strip(),
                    'Status': cols[5].text.strip()
                }
                all_votes.append(vote_data)
        
        # Check if we've reached the maximum number of pages to scrape
        if max_pages and page >= max_pages:
            break
        
        # Move to the next page
        page += 1
        
        # Add a small delay to avoid overloading the server
        time.sleep(1)
    
    # Convert to DataFrame
    df = pd.DataFrame(all_votes)
    print(f"Scraped a total of {len(df)} votes across {page} pages for {member_id}.")
    return df

def main():
    member_ids = ["B000825", "C001121", "C001137", "D000197", 
                  "E000300", "H001100", "N000191", "P000620"]
    
    # Scrape all members
    for member_id in member_ids:
        votes_df = scrape_votes(member_id, 1000)
        output_file = f"representative_{member_id}_votes.csv"
        votes_df.to_csv(output_file, index=False)
        print(f"Data saved to {output_file}\n")
    
    # Load all dataframes
    print("Loading and merging data...")
    all_votes_df = pd.read_csv("./representative_B000825_votes.csv")
    
    member_files = {
        'C001137': "./representative_C001137_votes.csv",
        'C001121': "./representative_C001121_votes.csv",
        'D000197': "./representative_D000197_votes.csv",
        'E000300': "./representative_E000300_votes.csv",
        'H001100': "./representative_H001100_votes.csv",
        'N000191': "./representative_N000191_votes.csv",
        'P000620': "./representative_P000620_votes.csv"
    }
    
    # Merge using left join on Roll Call Number
    # This handles duplicates by keeping all matches
    for member_id, filename in member_files.items():
        member_df = pd.read_csv(filename)
        # Select only Roll Call Number and the member's vote column
        member_votes = member_df[['Roll Call Number', member_id]].copy()
        
        # Remove duplicates keeping the first occurrence
        member_votes = member_votes.drop_duplicates(subset=['Roll Call Number'], keep='first')
        
        # Merge with the main dataframe
        all_votes_df = all_votes_df.merge(
            member_votes, 
            on='Roll Call Number', 
            how='left'
        )
    
    print(all_votes_df)
    output_file = "all_votes.csv"
    all_votes_df.to_csv(output_file, index=False)
    print(f"\nData saved to {output_file}")
    print(f"Total votes: {len(all_votes_df)}")
    print(f"Columns: {list(all_votes_df.columns)}")

if __name__ == "__main__":
    main()