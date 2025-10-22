
import requests
import logging
from typing import List, Dict, Any
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

class NDLClient:
    def __init__(self, timeout: int = 10):
        self.base_url = "https://iss.ndl.go.jp/api/sru"
        self.timeout = timeout

    def search_books(self, title: str, max_records: int = 10) -> List[Dict[str, Any]]:
        params = {
            "operation": "searchRetrieve",
            "query": f'title="{title}" AND mediatype=1',
            "maximumRecords": max_records,
            "recordPacking": "xml",
        }
        try:
            response = requests.get(self.base_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return self._parse_xml_response(response.text)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching data from NDL API: {e}")
            return []

    def _parse_xml_response(self, xml_string: str) -> List[Dict[str, Any]]:
        try:
            root = ET.fromstring(xml_string)
            records = []
            for record in root.findall(".//rss/channel/item"):
                link = record.find("link").text if record.find("link") is not None else None
                records.append({
                    "title": record.find("title").text if record.find("title") is not None else None,
                    "link": link,
                    "url": link,  # URLフィールドとしても利用できるようにする
                    "author": record.find("author").text if record.find("author") is not None else None,
                    "category": record.find("category").text if record.find("category") is not None else None,
                })
            return records
        except ET.ParseError as e:
            logger.error(f"Error parsing NDL API response: {e}")
            return []
