import sqlite3
import os
from datetime import datetime
from pathlib import Path

class DatabaseManager:
    def __init__(self, db_path='indexer.db'):
        self.db_path = db_path
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def init_db(self):
        """Initialize database with required tables."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Create indexed_items table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS indexed_items (
                    id INTEGER PRIMARY KEY,
                    item_type TEXT, -- 'python_app' or 'standalone_html'
                    name TEXT,
                    folder_path TEXT,
                    main_file_path TEXT,
                    html_interface_path TEXT,
                    thumbnail_path TEXT,
                    port INTEGER,
                    simple_id TEXT UNIQUE,    -- Simple ID like p001, h001, etc.

                    -- LLM Generated Content
                    description TEXT,        -- Rich description from LLM
                    short_desc TEXT,         -- 1-line summary
                    tech_stack TEXT,         -- Comma-separated tech list
                    tags TEXT,              -- Comma-separated tags
                    category TEXT,          -- Auto-categorized (web, api, tool, etc.)

                    -- File Analysis
                    file_size INTEGER,      -- Size in bytes
                    last_modified TIMESTAMP,
                    dependencies TEXT,      -- Key dependencies found

                    -- Metadata
                    created_at TIMESTAMP,
                    last_scanned TIMESTAMP,
                    llm_processed BOOLEAN DEFAULT 0,   -- Whether LLM analysis was done
                    is_favourite BOOLEAN DEFAULT 0     -- Favourite status
                )
            ''')

            # Add is_favourite column if it doesn't exist (for schema migration)
            cursor.execute("PRAGMA table_info(indexed_items)")
            columns = [row[1] for row in cursor.fetchall()]
            if 'is_favourite' not in columns:
                cursor.execute("ALTER TABLE indexed_items ADD COLUMN is_favourite BOOLEAN DEFAULT 0")

            # Add simple_id column if it doesn't exist
            if 'simple_id' not in columns:
                cursor.execute("ALTER TABLE indexed_items ADD COLUMN simple_id TEXT")

            # Add missing_dependencies column if it doesn't exist
            if 'missing_dependencies' not in columns:
                cursor.execute("ALTER TABLE indexed_items ADD COLUMN missing_dependencies BOOLEAN DEFAULT 0")

            # Create available_tags table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS available_tags (
                    tag TEXT PRIMARY KEY,
                    category TEXT,
                    usage_count INTEGER DEFAULT 0
                )
            ''')

            conn.commit()

    def _generate_simple_id(self, item_type):
        """Generate a simple ID like p001, h001, etc."""
        prefix = 'p' if item_type == 'python_app' else 'h'

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Find the highest existing ID for this type
            cursor.execute('''
                SELECT simple_id FROM indexed_items
                WHERE simple_id LIKE ?
                ORDER BY CAST(SUBSTR(simple_id, 2) AS INTEGER) DESC
                LIMIT 1
            ''', (f'{prefix}%',))

            row = cursor.fetchone()
            if row:
                # Extract number and increment
                existing_num = int(row[0][1:])
                new_num = existing_num + 1
            else:
                new_num = 1

            return f'{prefix}{new_num:03d}'

    def insert_item(self, item_data):
        """Insert or update an indexed item."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Check if item already exists
            cursor.execute('''
                SELECT id, last_modified, simple_id FROM indexed_items
                WHERE main_file_path = ? AND folder_path = ?
            ''', (item_data['main_file_path'], item_data['folder_path']))

            existing = cursor.fetchone()

            if existing:
                # Check if file has been modified
                stored_mtime = existing[1]
                current_mtime = item_data.get('last_modified')
                if current_mtime and stored_mtime and current_mtime != stored_mtime:
                    # Update existing item
                    last_scanned = datetime.now().isoformat()
                    columns = ', '.join(f"{k} = ?" for k in item_data.keys())
                    values = list(item_data.values()) + [last_scanned, existing[0]]
                    cursor.execute(f'''
                        UPDATE indexed_items SET {columns}, last_scanned = ?
                        WHERE id = ?
                    ''', values)
                else:
                    # Just update last_scanned
                    last_scanned = datetime.now().isoformat()
                    cursor.execute('UPDATE indexed_items SET last_scanned = ? WHERE id = ?', (last_scanned, existing[0]))
            else:
                # Generate simple_id for new item
                item_data['simple_id'] = self._generate_simple_id(item_data['item_type'])

                # Insert new item
                item_data['created_at'] = datetime.now().isoformat()
                item_data['last_scanned'] = item_data['created_at']
                columns = ', '.join(item_data.keys())
                placeholders = ', '.join('?' * len(item_data))
                values = list(item_data.values())
                cursor.execute(f'''
                    INSERT INTO indexed_items ({columns})
                    VALUES ({placeholders})
                ''', values)

            conn.commit()
            return cursor.lastrowid

    def get_all_items(self):
        """Get all indexed items."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM indexed_items ORDER BY name')
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def search_items(self, query, tags=None, category=None):
        """Search items by query, tags, and category."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            sql = '''
                SELECT * FROM indexed_items
                WHERE (name LIKE ? OR description LIKE ? OR tech_stack LIKE ? OR tags LIKE ?)
            '''
            params = [f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%']

            if tags:
                tag_conditions = ' OR '.join(['tags LIKE ?'] * len(tags))
                sql += f' AND ({tag_conditions})'
                params.extend([f'%{tag}%' for tag in tags])

            if category:
                sql += ' AND category = ?'
                params.append(category)

            sql += ' ORDER BY name'

            cursor.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_item_by_id(self, item_id):
        """Get a specific item by ID."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM indexed_items WHERE id = ?', (item_id,))
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    def get_item_by_html_path(self, html_path):
        """Get item by HTML interface path or main file path."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM indexed_items WHERE html_interface_path = ? OR main_file_path = ?', (html_path, html_path))
            row = cursor.fetchone()
            if row:
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row))
            return None

    def update_llm_data(self, item_id, llm_data):
        """Update LLM-generated data for an item."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE indexed_items
                SET description = ?, short_desc = ?, tech_stack = ?, tags = ?,
                    category = ?, llm_processed = 1
                WHERE id = ?
            ''', (
                llm_data.get('description'),
                llm_data.get('short_desc'),
                llm_data.get('tech_stack'),
                llm_data.get('tags'),
                llm_data.get('category'),
                item_id
            ))
            conn.commit()

    def get_available_tags(self):
        """Get all available tags with usage counts."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM available_tags ORDER BY usage_count DESC')
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def add_tag_usage(self, tags):
        """Increment usage count for tags."""
        if not tags:
            return

        with self.get_connection() as conn:
            cursor = conn.cursor()
            for tag in tags:
                cursor.execute('''
                    INSERT INTO available_tags (tag, usage_count)
                    VALUES (?, 1)
                    ON CONFLICT(tag) DO UPDATE SET usage_count = usage_count + 1
                ''', (tag,))
            conn.commit()

    def export_for_llm(self):
        """Export all items in JSON format for LLM processing."""
        items = self.get_all_items()
        return {
            'projects': [
                {
                    'id': item['id'],
                    'name': item['name'],
                    'type': item['item_type'],
                    'description': item.get('description', ''),
                    'tech_stack': item.get('tech_stack', ''),
                    'tags': item.get('tags', ''),
                    'main_file_path': item['main_file_path'],
                    'folder_path': item['folder_path'],
                    'file_size': item['file_size'],
                    'last_modified': item['last_modified']
                }
                for item in items
            ]
        }

    def cleanup_old_items(self, scanned_paths):
        """Remove items that are no longer present in scanned folders."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get all current items
            cursor.execute('SELECT id, main_file_path FROM indexed_items')
            existing_items = cursor.fetchall()

            to_remove = []
            for item_id, file_path in existing_items:
                if not Path(file_path).exists():
                    to_remove.append(item_id)

            if to_remove:
                cursor.executemany('DELETE FROM indexed_items WHERE id = ?', [(id,) for id in to_remove])
                conn.commit()
                return len(to_remove)

            return 0

    def remove_item(self, item_id):
        """Remove a single item by ID and delete its associated thumbnail."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get the thumbnail path for the item
            cursor.execute('SELECT thumbnail_path FROM indexed_items WHERE id = ?', (item_id,))
            row = cursor.fetchone()

            if row:
                thumbnail_path = row[0]
                # Delete thumbnail file if it exists
                if thumbnail_path and Path(thumbnail_path).exists():
                    try:
                        Path(thumbnail_path).unlink()
                        print(f"Deleted thumbnail: {thumbnail_path}")
                    except Exception as e:
                        print(f"Error deleting thumbnail {thumbnail_path}: {e}")

                # Delete database record
                cursor.execute('DELETE FROM indexed_items WHERE id = ?', (item_id,))
                conn.commit()
                return True
            else:
                return False

    def remove_folder_items(self, folder_path):
        """Remove all items from a specific folder and delete associated thumbnails."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Get all items in the folder
            cursor.execute('SELECT id, thumbnail_path FROM indexed_items WHERE folder_path = ?', (folder_path,))
            items = cursor.fetchall()

            removed_count = 0
            for item_id, thumbnail_path in items:
                # Delete thumbnail file if it exists
                if thumbnail_path and Path(thumbnail_path).exists():
                    try:
                        Path(thumbnail_path).unlink()
                        print(f"Deleted thumbnail: {thumbnail_path}")
                    except Exception as e:
                        print(f"Error deleting thumbnail {thumbnail_path}: {e}")

                # Delete database record
                cursor.execute('DELETE FROM indexed_items WHERE id = ?', (item_id,))
                removed_count += 1

            conn.commit()
            return removed_count

    def toggle_favourite(self, item_id):
        """Toggle the favourite status of an item."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE indexed_items
                SET is_favourite = NOT is_favourite
                WHERE id = ?
            ''', (item_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_favourite_items(self):
        """Get all favourite items."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM indexed_items WHERE is_favourite = 1 ORDER BY name')
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

# Global database instance
db = DatabaseManager()