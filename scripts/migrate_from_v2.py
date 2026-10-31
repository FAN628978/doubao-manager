#!/usr/bin/env python3
"""Optional offline migration: copy V2 'data' to a fresh V3 directory.

Both services MUST be stopped first. Never overwrites an existing V3 database.
This only copies local data; it never touches remote account services.
"""
import argparse
import shutil
import sqlite3
from pathlib import Path


def migrate(src, dst):
    src_data = src.resolve() / 'data'
    dst_data = dst.resolve() / 'data'
    source_db = src_data / 'manager.sqlite3'
    dest_db = dst_data / 'manager.sqlite3'
    if not source_db.is_file(): raise ValueError('未找到 V2 数据库 data/manager.sqlite3')
    if src_data == dst_data: raise ValueError('源目录和目标目录相同')
    if dest_db.exists(): raise ValueError('目标数据库已存在；为避免覆盖数据，拒绝迁移')
    for folder in ('profiles','videos','web_assets'):
        if (src_data/folder).exists() and (dst_data/folder).exists():
            raise ValueError('目标数据子目录已存在，拒绝覆盖：' + folder)
    dst_data.mkdir(parents=True, exist_ok=True)
    # SQLite backup produces a consistent snapshot, but migration requires services stopped.
    with sqlite3.connect(source_db) as source, sqlite3.connect(dest_db) as target:
        source.backup(target)
        cols = {r[1] for r in target.execute('PRAGMA table_info(accounts)')}
        if not {'id','profile_dir'}.issubset(cols): raise ValueError('数据库缺少账号目录字段，请人工检查')
        accounts = [r[0] for r in target.execute('SELECT id FROM accounts')]
        for acc in accounts:
            if not acc.startswith('acc_') or not all(x.isalnum() or x == '_' for x in acc):
                raise ValueError('检测到异常账号 ID，拒绝复制：' + acc)
            target.execute('UPDATE accounts SET profile_dir=? WHERE id=?',
                           (str(Path('data')/'profiles'/acc), acc))
        target.commit()
    for folder in ('profiles','videos','web_assets'):
        origin, target = src_data/folder, dst_data/folder
        if origin.exists():
            if target.exists(): raise ValueError('目标数据子目录已存在，拒绝覆盖：' + str(target))
            shutil.copytree(origin, target)
    return len(accounts)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='停止服务后，将旧 V2 数据迁移到全新 V3 工程目录')
    parser.add_argument('--from-dir',type=Path,required=True)
    parser.add_argument('--to-dir',type=Path,required=True)
    args=parser.parse_args()
    try:
        n=migrate(args.from_dir,args.to_dir)
        print(f'迁移完成：{n} 个账号档案。请检查 V3 中的资料目录和生成记录。')
    except Exception as e:
        print('迁移未完成：',e)
        raise SystemExit(1)
