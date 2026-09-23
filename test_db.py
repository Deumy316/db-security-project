import mysql.connector
import getpass

password = getpass.getpass("MySQL 비밀번호 입력: ")

try:
    conn = mysql.connector.connect(
        host="localhost", user="root", password=password, database="gigang_db"
    )

    print("DB 연결 성공!")

    cursor = conn.cursor()
    cursor.execute("SELECT DATABASE();")

    result = cursor.fetchone()
    print("현재 DB:", result[0])

except mysql.connector.Error as err:
    print("DB 연결 실패:", err)

finally:
    if "conn" in locals() and conn.is_connected():
        conn.close()
