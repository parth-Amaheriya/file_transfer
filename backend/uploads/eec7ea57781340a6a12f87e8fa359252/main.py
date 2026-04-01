from parser import apply_page_loop
from db import create_connection, create_database, create_table    
import time

if __name__=="__main__":
    start_time = time.time()
    
    try:
        connection = create_connection()
        create_database(connection)
        create_table(connection)
        apply_page_loop('https://stores.burgerking.in/?page={}',connection)
    except Exception as e:
        print(e)

    end_time = time.time()
    print(f"Execution time: {end_time - start_time} seconds")