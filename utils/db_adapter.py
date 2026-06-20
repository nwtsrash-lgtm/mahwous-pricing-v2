"""
utils/db_adapter.py - محول قاعدة البيانات الموحد (MySQL/SQLite)
يدعم الحفظ الدائم في Google Cloud SQL مع ميزة الكتالوج الدائم.
"""
import os
import sqlite3
import pandas as pd
import mysql.connector
from mysql.connector import Error
from .data_paths import get_data_db_path

def get_db_connection():
    """الحصول على اتصال بقاعدة البيانات (MySQL إن وجد، وإلا SQLite)."""
    # إعدادات MySQL من متغيرات البيئة
    db_user = os.environ.get("DB_USER")
    db_pass = os.environ.get("DB_PASS")
    db_name = os.environ.get("DB_NAME")
    cloud_sql_connection_name = os.environ.get("CLOUD_SQL_CONNECTION_NAME")
    
    # إذا كانت إعدادات MySQL متوفرة (بيئة Cloud Run)
    if all([db_user, db_pass, db_name]) and cloud_sql_connection_name:
        try:
            # الاتصال عبر Unix Socket (Cloud Run default)
            unix_socket = f"/cloudsql/{cloud_sql_connection_name}"
            conn = mysql.connector.connect(
                user=db_user,
                password=db_pass,
                database=db_name,
                unix_socket=unix_socket
            )
            return conn, "mysql"
        except Error as e:
            print(f"خطأ في الاتصال بـ MySQL: {e}. سيتم الرجوع لـ SQLite.")
    
    # الرجوع لـ SQLite كبديل (أو للتطوير المحلي)
    db_path = get_data_db_path("mahwous_smart_pricing.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return conn, "sqlite"

def init_db():
    """إنشاء الجداول اللازمة إذا لم تكن موجودة."""
    conn, db_type = get_db_connection()
    cursor = conn.cursor()
    
    # جدول المنتجات المستخرجة (الكتالوج الدائم)
    if db_type == "mysql":
        create_table_query = """
        CREATE TABLE IF NOT EXISTS scraped_products (
            id INT AUTO_INCREMENT PRIMARY KEY,
            store_name VARCHAR(255),
            product_name TEXT,
            price DECIMAL(10, 2),
            url TEXT,
            image_url TEXT,
            sku VARCHAR(100),
            category VARCHAR(100),
            brand VARCHAR(100),
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX(store_name),
            INDEX(sku)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
    else:
        create_table_query = """
        CREATE TABLE IF NOT EXISTS scraped_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT,
            product_name TEXT,
            price REAL,
            url TEXT,
            image_url TEXT,
            sku TEXT,
            category TEXT,
            brand TEXT,
            scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    
    cursor.execute(create_table_query)
    conn.commit()
    cursor.close()
    conn.close()

def save_products_to_db(df, store_name):
    """حفظ مجموعة منتجات في قاعدة البيانات."""
    if df is None or df.empty:
        return
    
    conn, db_type = get_db_connection()
    
    # إضافة اسم المتجر للبيانات
    df_to_save = df.copy()
    df_to_save['store_name'] = store_name
    
    if db_type == "mysql":
        # استخدام pandas للرفع السريع لـ MySQL
        # ملاحظة: يحتاج sqlalchemy لـ pandas.to_sql مع mysql
        # كحل بديل بسيط وسريع بدون مكتبات إضافية:
        cursor = conn.cursor()
        for _, row in df_to_save.iterrows():
            insert_query = """
            INSERT INTO scraped_products (store_name, product_name, price, url, image_url, sku, category, brand)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(insert_query, (
                row.get('store_name'), row.get('product_name'), row.get('price'),
                row.get('url'), row.get('image_url'), row.get('sku'),
                row.get('category'), row.get('brand')
            ))
        conn.commit()
        cursor.close()
    else:
        df_to_save.to_sql('scraped_products', conn, if_exists='append', index=False)
    
    conn.close()

def get_all_scraped_products():
    """جلب كافة المنتجات المحفوظة."""
    conn, db_type = get_db_connection()
    query = "SELECT * FROM scraped_products ORDER BY scraped_at DESC"
    df = pd.read_sql(query, conn)
    conn.close()
    return df
