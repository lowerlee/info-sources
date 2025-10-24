from bs4 import BeautifulSoup
from readability import Document
import html2text
import requests
import json
import os
import time
from urllib.parse import urlparse
import re

def get_full_article(html_string):
    """
    Extract full article content handling heterogeneous HTML structures
    """
    # Step 1: Use readability to identify main content
    doc = Document(html_string)
    main_content_html = doc.summary()
    
    # Step 2: Parse with BeautifulSoup for cleanup
    soup = BeautifulSoup(main_content_html, 'html.parser')
    
    # Remove unwanted elements
    for element in soup.find_all(['script', 'style', 'nav', 'footer']):
        element.decompose()
    
    # Step 3: Convert to markdown or text
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.body_width = 0
    
    return converter.handle(str(soup))

def sanitize_filename(title):
    """Convert title to safe filename"""
    # Remove/replace invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', title)
    filename = re.sub(r'\s+', '_', filename)
    # Limit length
    return filename[:100]

def fetch_article_content(url, timeout=30):
    """Fetch HTML content from URL"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        print(f"Error fetching {url}: {e}")
        return None

def process_articles():
    """Process all articles from JSON and save as markdown"""
    
    # Load articles data
    json_path = '../data/bpc_blogs.json'
    if not os.path.exists(json_path):
        print(f"Error: {json_path} not found. Run bpc-blog.py first.")
        return
    
    with open(json_path, 'r', encoding='utf-8') as f:
        articles = json.load(f)
    
    # Create output directory
    output_dir = '../data/markdown_articles'
    os.makedirs(output_dir, exist_ok=True)
    
    print(f"Processing {len(articles)} articles...")
    
    successful = 0
    failed = 0
    
    for i, article in enumerate(articles):
        # Get article details
        title = article.get('title', 'Untitled')
        date = article.get('date', '')
        url = article.get('permalink', '')
        
        if not url:
            print(f"Skipping article {i+1}: No URL found")
            failed += 1
            continue
        
        print(f"Processing {i+1}/{len(articles)}: {title[:50]}...")
        
        # Fetch HTML content
        html_content = fetch_article_content(url)
        if not html_content:
            failed += 1
            continue
        
        # Extract article content
        try:
            markdown_content = get_full_article(html_content)
            
            # Create markdown with frontmatter
            frontmatter = f"""---
title: "{title}"
date: "{date}"
url: "{url}"
type: "{article.get('type', '')}"
tags: {json.dumps(article.get('tags', []))}
policy_areas: {json.dumps(article.get('policy_areas', []))}
related_people: {json.dumps(article.get('related_people', []))}
---

"""
            
            full_markdown = frontmatter + markdown_content
            
            # Save to file
            filename = f"{sanitize_filename(title)}.md"
            filepath = os.path.join(output_dir, filename)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(full_markdown)
            
            successful += 1
            print(f"  ✓ Saved: {filename}")
            
        except Exception as e:
            print(f"  ✗ Error processing article: {e}")
            failed += 1
        
        # Rate limiting
        time.sleep(0.5)
    
    print(f"\nCompleted: {successful} successful, {failed} failed")

if __name__ == "__main__":
    process_articles()