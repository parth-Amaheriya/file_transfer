

import requests
from lxml import html
import time

from db import insert_link
from models import StoreRecord

BATCH_SIZE=50

def insert_batch(records,connection):
    if not records:
        return
    try:
        insert_link(connection, [record.as_dict() for record in records])
    except Exception as exc:
        print(f"Failed to insert batch: {exc}")

def extract_link(tree):
    records=[]
    sections=tree.xpath("//div[@class='store-info-box']")

    for section in sections:
        links=section.xpath('.//a[@title]/@href')
        link=links[0] if links else None

        spans=section.xpath('.//div[@class="info-text"]/span')
        texts=[span.text_content().strip() for span in spans] if spans else []
        address=' '.join(texts[:2]) if len(texts) >= 2 else (texts[0] if texts else None)
        state=texts[2] if len(texts) >= 3 else None

        numbers=section.xpath('.//a[starts-with(@href, "tel:")]/@href')
        phone=numbers[0].replace("tel:", "").strip() if numbers else None

        time_raw=section.xpath('normalize-space(.//li[@class="outlet-timings"]//span/text())')
        time_value=time_raw or None

        records.append(StoreRecord(
            link=link,
            address=address,
            state=state,
            phone=phone,
            time=time_value,
        ))
    return records


def apply_page_loop(url_template: str,connection):
    headers={
        "User-Agent": (
            "Mozilla/4.0 (Windows NT 11.0; Win64; x64)" 
        )
    }

    batch=[]
    page=1

    while True:
        url=url_template.format(page)
        try:
            start_time = time.time()
            response=requests.get(url, headers=headers)
            print(f" {time.time() - start_time} seconds")
            print(response, url)

            if response.status_code != 200:
                print(f"Received status {response.status_code} for page {page}")
                break

            tree=html.fromstring(response.content)
            if tree.xpath("//span[@class='causion-icon']"):
                print(f"No more pages to fetch at page {page}")
                break
            
            result=extract_link(tree)
    
        except Exception as exc:
            print(f"Failed to fetch page {page}: {exc}")
            break


        batch.extend(result)

        if len(batch) >= BATCH_SIZE:
            insert_batch(batch.copy(), connection)
            batch.clear()

        page += 1

    if batch:
        insert_batch(batch,connection)