import mysql.connector

DATABASE="burger_king_linkes"

def create_connection(args:dict=None):
    confi={
            "host": "localhost",
            "user": "root",
            "password": "actowiz"
        }
    if args is None:
        args=confi
    else:
        args.update(confi)    
    connection=mysql.connector.connect(**args)
    return connection   


def create_database(connection):
    cursor=connection.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
    print("Database created successfully")

def create_table(connection):
    cursor=connection.cursor()
    cursor.execute(f"USE {DATABASE}")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS links (
            id INT AUTO_INCREMENT PRIMARY KEY,
            link VARCHAR(255),
            address VARCHAR(512),
            state VARCHAR(128),
            phone VARCHAR(64),
            timings VARCHAR(255)
        )
    """)
    print("Table created successfully")

def insert_link(connection, stores):
    if not stores:
        return

    cursor=connection.cursor()
    data=[
        (
            store.get("link"),
            store.get("address"),
            store.get("state"),
            store.get("phone"),
            store.get("time"),
        )
        for store in stores
    ]

    cursor.executemany(
        """
        INSERT INTO links (link, address, state, phone, timings)
        VALUES (%s, %s, %s, %s, %s)
        """,
        data
    )
    connection.commit()
    cursor.close()
    print(f"{len(data)} records inserted successfully")
    
