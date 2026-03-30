
import mysql.connector
import json

FOLDER_PATH = "files/PDP/PDP"
OUTPUT_FOLDER_PATH="output_files"

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "actowiz",
}

DATABASE = 'grabfood_db'


def get_connection():
    return mysql.connector.connect(**DB_CONFIG)

def get_connection_thread():
    return mysql.connector.connect(**{**DB_CONFIG,"database":DATABASE})



def create_database(cursor):
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
    cursor.execute(f"USE {DATABASE}")

def create_table(cursor):
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS restaurant_data (
        id INT AUTO_INCREMENT PRIMARY KEY,
        restaurant_name TEXT,
        product_category TEXT,
        product_img TEXT,
        
        latitude DOUBLE,
        longitude DOUBLE,
        
        time_zone TEXT,
        currency TEXT,
        delivery_time INT,
        rating DOUBLE,
        
        deliverable_distance DOUBLE,
        
        availability JSON,
        menu JSON
    )
    """)


def insert_multiple_data(cursor, grab_food_list):
    if not grab_food_list:
        return

    query = """
    INSERT INTO restaurant_data (
        restaurant_name,
        product_category,
        product_img,
        latitude,
        longitude,
        time_zone,
        currency,
        delivery_time,
        rating,
        deliverable_distance,
        availability,
        menu
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    rows = []
    for gf in grab_food_list:
        rows.append((
            gf.restaurant_name,
            gf.product_category,
            gf.img,
            gf.location.latitude,
            gf.location.longitude,
            gf.timeZone,
            gf.currency,
            gf.delivery_time,
            gf.rating,
            gf.deliverable_distance,
            json.dumps([a.model_dump() for a in gf.availability]),
            json.dumps([m.model_dump() for m in gf.menu])
        ))

    cursor.executemany(query, rows)