#!/usr/bin/env python3
import firebase_admin
from firebase_admin import credentials, firestore
import json
from datetime import datetime
import os


def serialize_value(obj):
    """将 Firestore 特殊类型转换为可序列化的格式"""
    if hasattr(obj, 'timestamp_pb'):  # DatetimeWithNanoseconds
        return obj.isoformat()
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: serialize_value(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize_value(item) for item in obj]
    return obj

# os.environ['HTTP_PROXY'] = 'http://127.0.0.1:7897'
# os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:7897'

print("📋 开始初始化 Firebase...")
cred = credentials.Certificate('first-0521lv-firebase-adminsdk-fbsvc-9fad316444.json')
print("✓ 加载凭证文件成功")

firebase_admin.initialize_app(cred)
print("✓ Firebase 初始化完成")

db = firestore.client()
print("✓ Firestore 客户端已连接")

print("\n📊 开始获取数据...")
data = {}
for collection in db.collections():
    print(f"  → 处理集合: {collection.id}")
    docs = list(collection.stream())
    data[collection.id] = [{'id': doc.id, 'data': serialize_value(doc.to_dict())} for doc in docs]
    print(f"    ✓ 获取 {len(docs)} 条文档")

print(f"\n✓ 共获取 {len(data)} 个集合")
print("\n📄 数据内容:")
print(json.dumps(data, indent=2, ensure_ascii=False))

print("\n💾 保存数据到文件...")
with open('data.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
print("✓ 数据已保存到 data.json")
