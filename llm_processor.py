import os
import re
from pathlib import Path
from typing import Dict, List, Optional
import ollama
from database import db

class LLMProcessor:
    def __init__(self):
        # Ollama runs locally, no API key needed
        pass

    def process_item(self, item_id: int) -> Dict:
        """Process a single item to generate LLM metadata."""
        item = db.get_item_by_id(item_id)
        if not item:
            return {}

        # Gather context
        context = self._gather_context(item)

        # Generate metadata
        metadata = self._generate_metadata(context)

        # Update database
        if metadata:
            db.update_llm_data(item_id, metadata)

        return metadata

    def _gather_context(self, item) -> Dict:
        """Gather all relevant context for LLM processing."""
        context = {
            'name': item['name'],
            'type': item['item_type'],
            'main_file_path': item['main_file_path'],
            'folder_path': item['folder_path']
        }

        # Priority 1: Read .md files (README, docs, etc.)
        md_content = self._find_and_read_md_files(item['folder_path'])
        if md_content:
            context['primary_content'] = md_content[:2500]  # Prioritize .md files
            context['content_type'] = 'markdown'
        else:
            # Priority 2: Fallback to main file content
            main_content = self._read_file_content(item['main_file_path'])
            context['primary_content'] = main_content[:2000]  # Limit size
            context['content_type'] = 'code'

        # Extract code features from main file (always useful)
        main_content = self._read_file_content(item['main_file_path'])
        context['code_features'] = self._extract_code_features(main_content)

        # Find related files
        context['related_files'] = self._find_related_files(item['folder_path'])

        return context

    def _read_file_content(self, file_path: str) -> str:
        """Read file content safely."""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.read()
        except Exception as e:
            print(f"Error reading {file_path}: {e}")
            return ""

    def _find_and_read_md_files(self, folder_path: str) -> str:
        """Find and read all .md files in project folder, prioritizing README files."""
        folder = Path(folder_path)
        md_content = []

        # Priority 1: README files in root
        readme_candidates = ['README.md', 'readme.md', 'README.MD', 'readme.MD']
        for candidate in readme_candidates:
            readme_path = folder / candidate
            if readme_path.exists():
                content = self._read_file_content(str(readme_path))
                if content:
                    md_content.append(f"README: {content[:1000]}")  # Limit each file

        # Priority 2: Other .md files in root
        for md_file in folder.glob('*.md'):
            if md_file.name.lower() not in [c.lower() for c in readme_candidates]:
                content = self._read_file_content(str(md_file))
                if content:
                    md_content.append(f"{md_file.name}: {content[:800]}")  # Limit each file

        # Priority 3: .md files in docs/ or other common folders
        docs_folders = ['docs', 'documentation', 'wiki', '.github']
        for docs_dir in docs_folders:
            docs_path = folder / docs_dir
            if docs_path.exists():
                for md_file in docs_path.rglob('*.md'):
                    # Skip node_modules
                    if 'node_modules' in str(md_file):
                        continue
                    content = self._read_file_content(str(md_file))
                    if content:
                        md_content.append(f"{md_file.relative_to(folder)}: {content[:600]}")  # Limit each file

        return '\n\n'.join(md_content) if md_content else ""

    def _extract_code_features(self, code_content: str) -> Dict:
        """Extract features from code content."""
        features = {
            'imports': [],
            'functions': [],
            'classes': [],
            'comments': []
        }

        # Extract imports
        import_pattern = r'^(?:import|from)\s+([^\n]+)'
        features['imports'] = re.findall(import_pattern, code_content, re.MULTILINE)

        # Extract functions
        func_pattern = r'def\s+(\w+)\s*\('
        features['functions'] = re.findall(func_pattern, code_content)

        # Extract classes
        class_pattern = r'class\s+(\w+)'
        features['classes'] = re.findall(class_pattern, code_content)

        # Extract comments
        comment_pattern = r'#\s*([^\n]+)'
        features['comments'] = re.findall(comment_pattern, code_content)[:10]  # Limit

        return features

    def _find_related_files(self, folder_path: str) -> List[str]:
        """Find related files in the project."""
        folder = Path(folder_path)
        related = []

        # Common config files
        config_files = ['requirements.txt', 'package.json', 'setup.py', 'pyproject.toml', 'Pipfile']
        for config in config_files:
            if (folder / config).exists():
                related.append(config)

        # Find other Python files
        py_files = [f for f in folder.rglob('*.py') if 'node_modules' not in str(f)][:5]  # Limit, skip node_modules
        related.extend([str(f.relative_to(folder)) for f in py_files])

        return related[:10]  # Limit total

    def _generate_metadata(self, context: Dict) -> Dict:
        """Generate metadata using Ollama."""
        try:
            prompt = self._build_prompt(context)

            response = ollama.chat(
                model='granite4:micro-h',
                messages=[
                    {"role": "system", "content": "You are an expert software analyst. Analyze the provided code and documentation to generate metadata for a web application indexer."},
                    {"role": "user", "content": prompt}
                ],
                options={
                    'temperature': 0.2,
                    'num_predict': 1000
                }
            )

            result = response['message']['content'].strip()
            return self._parse_llm_response(result)

        except Exception as e:
            print(f"Error generating LLM metadata: {e}")
            return {}

    def _build_prompt(self, context: Dict) -> str:
        """Build the prompt for LLM."""
        content_type_desc = "Markdown documentation" if context['content_type'] == 'markdown' else "Source code"

        prompt = f"""
Analyze this {context['type']} project and generate metadata:

Project Name: {context['name']}

{content_type_desc} Content (first 2500 chars):
{context['primary_content']}

Code Features:
- Imports: {', '.join(context['code_features']['imports'][:10])}
- Functions: {', '.join(context['code_features']['functions'][:10])}
- Classes: {', '.join(context['code_features']['classes'][:5])}
- Comments: {', '.join(context['code_features']['comments'][:5])}

Related Files: {', '.join(context['related_files'][:10])}

Please provide:
1. Description: Rich description (2-3 sentences)
2. Short Description: One-line summary
3. Tech Stack: Comma-separated technologies
4. Tags: Comma-separated relevant tags
5. Category: One of (web, api, tool, other)

Format your response as:
Description: [description]
Short: [short desc]
Tech: [tech1, tech2, tech3]
Tags: [tag1, tag2, tag3]
Category: [category]
"""
        return prompt

    def _parse_llm_response(self, response: str) -> Dict:
        """Parse the LLM response into structured data."""
        metadata = {}

        # Simple parsing - could be improved with more robust parsing
        lines = response.split('\n')
        for line in lines:
            line = line.strip()
            if line.startswith('Description:'):
                metadata['description'] = line.replace('Description:', '').strip()
            elif line.startswith('Short:'):
                metadata['short_desc'] = line.replace('Short:', '').strip()
            elif line.startswith('Tech:'):
                tech_str = line.replace('Tech:', '').strip()
                metadata['tech_stack'] = tech_str
            elif line.startswith('Tags:'):
                tags_str = line.replace('Tags:', '').strip()
                metadata['tags'] = tags_str
            elif line.startswith('Category:'):
                metadata['category'] = line.replace('Category:', '').strip().lower()

        return metadata

    def process_all_unprocessed(self) -> int:
        """Process all items that haven't been processed by LLM yet."""
        items = db.get_all_items()
        unprocessed = [item for item in items if not item.get('llm_processed')]

        processed_count = 0
        for item in unprocessed:
            if self.process_item(item['id']):
                processed_count += 1

        return processed_count

    def reprocess_item(self, item_id: int) -> Dict:
        """Reprocess an item even if already processed."""
        # Reset llm_processed flag
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE indexed_items SET llm_processed = 0 WHERE id = ?', (item_id,))
            conn.commit()

        return self.process_item(item_id)