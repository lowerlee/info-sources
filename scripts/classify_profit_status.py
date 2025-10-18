#!/usr/bin/env python3
"""
Automated Profit Status Classification Tool

This script automatically determines the profit status of information sources
by analyzing organization names, domains, and fetching additional data.
"""

import csv
import re
import requests
from urllib.parse import urlparse
import time
from typing import Dict, List, Tuple, Optional
import json

class ProfitStatusClassifier:
    def __init__(self):
        # Known patterns for classification
        self.government_patterns = [
            # US Government
            r'\.gov$', r'house\.gov', r'senate\.gov', r'congress\.gov',
            # Other government indicators
            r'department of', r'ministry of', r'bureau of', r'office of',
            r'customs and border', r'veterans affairs', r'homeland security'
        ]
        
        self.nonprofit_keywords = [
            'institute', 'foundation', 'center', 'society', 'association',
            'council', 'coalition', 'project', 'organization', 'archive',
            'research', 'policy', 'studies', 'watch', 'international',
            'endowment', 'commission'
        ]
        
        self.forprofit_indicators = [
            # News/Media companies
            'news', 'times', 'post', 'journal', 'magazine', 'media',
            'broadcasting', 'network', 'press', 'wire', 'daily',
            # Business indicators
            'inc.', 'corp', 'corporation', 'company', 'llc', 'ltd'
        ]
        
        # Known classifications (manual overrides)
        self.known_classifications = {
            # Government
            'congressional research service': 'government',
            'white house': 'government',
            'us customs and border protection': 'government',
            'department of health and human services': 'government',
            'small business administration': 'government',
            'national science foundation': 'government',
            'department of veterans affairs': 'government',
            
            # Major news outlets (for-profit)
            'associated press': 'for-profit',
            'reuters': 'for-profit',
            'politico': 'for-profit',
            'the hill': 'for-profit',
            'newsmax': 'for-profit',
            'newsweek': 'for-profit',
            'foreign policy': 'for-profit',
            
            # Major nonprofits
            'propublica': 'non-profit',
            'pew research': 'non-profit',
            'brookings institute': 'non-profit',
            'heritage foundation': 'non-profit',
            'amnesty international': 'non-profit',
            'freedom house': 'non-profit',
            'transparency international': 'non-profit',
            'human rights watch': 'non-profit',
            'aclu': 'non-profit',
            'american civil liberties union': 'non-profit'
        }

    def classify_by_domain(self, url: str) -> Optional[str]:
        """Classify based on domain patterns"""
        try:
            domain = urlparse(url).netloc.lower()
            
            # Government domains
            for pattern in self.government_patterns:
                if re.search(pattern, domain):
                    return 'government'
            
            # Educational institutions (usually non-profit)
            if domain.endswith('.edu'):
                return 'non-profit'
                
            return None
        except:
            return None

    def classify_by_name(self, name: str) -> Optional[str]:
        """Classify based on organization name patterns"""
        name_lower = name.lower()
        
        # Check known classifications first
        for known_name, classification in self.known_classifications.items():
            if known_name in name_lower:
                return classification
        
        # Check for government indicators
        if any(keyword in name_lower for keyword in ['house ', 'senate ', 'committee', 'commission']):
            if any(keyword in name_lower for keyword in ['house', 'senate', 'congressional', 'joint']):
                return 'government'
        
        # Count nonprofit indicators
        nonprofit_score = sum(1 for keyword in self.nonprofit_keywords if keyword in name_lower)
        
        # Count for-profit indicators
        forprofit_score = sum(1 for keyword in self.forprofit_indicators if keyword in name_lower)
        
        # Decision logic
        if nonprofit_score >= 2:
            return 'non-profit'
        elif forprofit_score >= 1:
            return 'for-profit'
        elif nonprofit_score >= 1:
            return 'non-profit'
            
        return None

    def classify_source(self, name: str, url: str) -> str:
        """Main classification function"""
        # Try domain classification first
        domain_result = self.classify_by_domain(url)
        if domain_result:
            return domain_result
        
        # Try name classification
        name_result = self.classify_by_name(name)
        if name_result:
            return name_result
        
        # Default to unknown if we can't classify
        return 'unknown'

    def process_csv(self, input_file: str, output_file: str):
        """Process the CSV file and add profit status classifications"""
        results = []
        
        with open(input_file, 'r', encoding='utf-8') as file:
            reader = csv.DictReader(file)
            
            for row in reader:
                name = row['name']
                url = row['url']
                
                # Classify the source
                profit_status = self.classify_source(name, url)
                
                # Add profit_status to the row
                row['profit_status'] = profit_status
                results.append(row)
                
                print(f"Classified: {name[:50]:<50} -> {profit_status}")
        
        # Write results to output file
        if results:
            fieldnames = list(results[0].keys())
            with open(output_file, 'w', newline='', encoding='utf-8') as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)
        
        return results

    def generate_report(self, results: List[Dict]) -> Dict:
        """Generate a classification report"""
        status_counts = {}
        for result in results:
            status = result['profit_status']
            status_counts[status] = status_counts.get(status, 0) + 1
        
        total = len(results)
        report = {
            'total_sources': total,
            'classifications': status_counts,
            'percentages': {status: round(count/total*100, 1) 
                          for status, count in status_counts.items()}
        }
        
        return report

def main():
    classifier = ProfitStatusClassifier()
    
    input_file = '/home/kevin/Projects/profit-status/data/info-sources - main.csv'
    output_file = '/home/kevin/Projects/profit-status/data/info-sources - classified.csv'
    
    print("Starting automated profit status classification...")
    print("=" * 60)
    
    # Process the CSV
    results = classifier.process_csv(input_file, output_file)
    
    # Generate report
    report = classifier.generate_report(results)
    
    print("\n" + "=" * 60)
    print("CLASSIFICATION REPORT")
    print("=" * 60)
    print(f"Total sources processed: {report['total_sources']}")
    print("\nBreakdown by profit status:")
    for status, count in report['classifications'].items():
        percentage = report['percentages'][status]
        print(f"  {status.title():<12}: {count:>3} ({percentage:>5.1f}%)")
    
    print(f"\nClassified data saved to: {output_file}")
    
    # Save detailed report
    report_file = '/home/kevin/Projects/profit-status/data/classification-report.json'
    with open(report_file, 'w') as f:
        json.dump(report, f, indent=2)
    print(f"Detailed report saved to: {report_file}")

if __name__ == "__main__":
    main()