#!/usr/bin/env python3
"""
週刊Life is beautiful メルマガ自動取得・分類スクリプト（APIキー不要版）
毎週火曜日にGitHub Actionsから実行される
"""
import os
import json
import base64
import re
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
DATA_PATH = 'newsletter/data.json'
SENDER = 'mailmag@mag2premium.com'

# ── キーワードによるカテゴリ分類 ──
CATEGORY_KEYWORDS = {
    'ai-llm':   ['AI', 'LLM', 'Claude', 'GPT', 'ChatGPT', 'Anthropic', 'OpenAI',
                 'エージェント', '言語モデル', 'トークン', 'ニューラルネット', '機械学習',
                 'Gemini', 'xAI', 'Grok', 'DeepSeek', 'バイブコーディング',
                 'Kimi', 'スケーリング', 'フロンティアモデル', 'RLHF', 'RAG'],
    'software': ['プログラム', 'コード', '開発', 'ソフトウェア', 'アーキテクチャ',
                 'API', 'GitHub', 'Python', 'TypeScript', 'アプリ', 'SaaS',
                 'MulmoClaude', 'MulmoChat', 'MulmoCast', 'Claude Code',
                 'エンジニア', 'リファクタリング', 'データベース', 'サーバー'],
    'business': ['企業', 'スタートアップ', 'CEO', 'CTO', 'M&A', '買収', '経営',
                 'ビジネス', '株価', '投資家', 'VC', '上場', '時価総額',
                 'リクルート', '採用', 'ユニコーン', '転職'],
    'economy':  ['経済', '雇用', '労働', '賃金', '市場', '消費', 'GDP',
                 'インフレ', '株式', '損失', '利益', 'billion', '兆円',
                 '億ドル', '格差', '所得'],
    'infra':    ['データセンター', '半導体', 'GPU', 'CPU', '電力', 'インフラ',
                 'NVIDIA', 'AMD', 'エネルギー', '発電', 'クラウド', 'AWS',
                 'Azure', 'Google Cloud', 'コンピュータ', 'メモリ', 'チップ'],
    'robot':    ['ロボット', 'ヒューマノイド', '自動運転', 'ドローン', 'Figure',
                 '人型', '機械', 'Physical AI', 'UUV', '水中'],
    'science':  ['科学', '医療', 'バイオ', '研究', '論文', 'ゲノム', '細胞',
                 'がん', '創薬', 'DNA', '長寿', 'ペプチド', 'iPS',
                 '生物学', '進化', '遺伝子', 'Mayo Clinic', 'Altos Labs'],
    'policy':   ['規制', '政策', '法律', '政府', '議会', '大統領', '安全保障',
                 '地政学', '裁判', '判決', 'トランプ', '中国', '米中',
                 '国防', 'ペンタゴン', '国家'],
    'personal': ['旅行', 'コンサート', '食事', '散歩', '滞在', 'ゴルフ',
                 'ざっくばらん', 'クルーズ', 'ハワイ', 'スペイン', 'モロッコ',
                 'シアトル', '明治神宮', 'ラン・ラン'],
    'qa':       ['質問コーナー', 'Q&A', '【質問】', '《回答》'],
}


def score_category(text):
    """テキストに含まれるキーワード数でカテゴリを判定する"""
    scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    text_lower = text.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else 'personal'


def trim_text(text, max_chars=200):
    """テキストを指定文字数で切り詰める"""
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= max_chars:
        return text
    # 句点・。で区切って自然に切る
    for end in ['。', '．', '. ']:
        pos = text.rfind(end, 0, max_chars)
        if pos > max_chars // 2:
            return text[:pos + len(end)]
    return text[:max_chars] + '…'


def get_gmail_service():
    token_data = json.loads(os.environ['GMAIL_TOKEN'])
    creds = Credentials(
        token=token_data.get('token'),
        refresh_token=token_data['refresh_token'],
        token_uri=token_data['token_uri'],
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=token_data['scopes'],
    )
    if not creds.valid:
        creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds)


def get_message_body(service, message_id):
    msg = service.users().messages().get(
        userId='me', id=message_id, format='full'
    ).execute()

    def decode_part(part):
        if part.get('mimeType') == 'text/html':
            data = part.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
        for sub in part.get('parts', []):
            result = decode_part(sub)
            if result:
                return result
        return None

    payload = msg.get('payload', {})
    headers = {h['name']: h['value'] for h in payload.get('headers', [])}
    subject = headers.get('Subject', '')
    html_body = decode_part(payload)
    return subject, html_body


def parse_issue_info(subject):
    m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日号', subject)
    if not m:
        return None
    year, month, day = m.groups()
    issue_id = f"{year}{int(month):02d}{int(day):02d}"
    return {
        'id': issue_id,
        'label': f"{year}年{int(month)}月{int(day)}日号",
        'shortLabel': f"{int(month)}/{int(day)}",
        'date': f"{year}-{int(month):02d}-{int(day):02d}",
    }


def extract_articles(html_body, issue_info, next_id):
    """HTMLを解析してトピックを抽出する（APIなし）"""
    soup = BeautifulSoup(html_body, 'html.parser')

    # フッターを除去
    for tag in soup.select('table#mag2-pay-magazine-footer, hr'):
        tag.decompose()

    articles = []
    current_id = next_id

    # h1タグでセクションを区切る
    sections = {}
    current_h1 = None
    for tag in soup.find_all(['h1', 'h2', 'p', 'a']):
        if tag.name == 'h1':
            current_h1 = tag.get_text(strip=True)
            sections.setdefault(current_h1, [])
        elif current_h1:
            sections[current_h1].append(tag)

    for h1_title, tags in sections.items():

        # ── 質問コーナー（まとめて1件）──
        if '質問' in h1_title or 'Q&A' in h1_title.upper():
            # 最初の質問だけタイトルとして抜粋
            q_texts = [t.get_text(strip=True) for t in tags
                       if t.name == 'p' and '【質問】' in t.get_text()]
            summary = '読者からの質問と中島氏の回答コーナー。'
            if q_texts:
                summary += '今号の質問テーマ：' + '、'.join(
                    q[:20] for q in q_texts[:3]
                )
            articles.append({
                'id': current_id,
                'issue': issue_info['id'],
                'cat': 'qa',
                'title': f'質問コーナー（{issue_info["label"]}）',
                'summary': summary,
                'detail': '',
                'url': '',
            })
            current_id += 1
            continue

        # ── 今週のざっくばらん ──
        if 'ざっくばらん' in h1_title:
            # h2タグがサブトピック
            subtopics = []
            current_sub = None
            sub_paras = []
            for tag in tags:
                if tag.name == 'h2':
                    if current_sub and sub_paras:
                        subtopics.append((current_sub, sub_paras))
                    current_sub = tag.get_text(strip=True)
                    sub_paras = []
                elif tag.name == 'p' and current_sub:
                    text = tag.get_text(strip=True)
                    if text:
                        sub_paras.append(text)
            if current_sub and sub_paras:
                subtopics.append((current_sub, sub_paras))

            for title, paras in subtopics:
                body = ' '.join(paras)
                articles.append({
                    'id': current_id,
                    'issue': issue_info['id'],
                    'cat': score_category(title + ' ' + body),
                    'title': title,
                    'summary': trim_text(body, 200),
                    'detail': trim_text(body, 500) if len(body) > 200 else '',
                    'url': '',
                })
                current_id += 1
            continue

        # ── 私の目に留まった記事 ──
        if '記事' in h1_title or '目に留まった' in h1_title:
            # <p><a href="...">タイトル</a></p> + 後続段落が1記事
            i = 0
            while i < len(tags):
                tag = tags[i]
                link = tag.find('a') if tag.name == 'p' else None
                if link and link.get('href', '').startswith('http'):
                    url = link['href']
                    title = link.get_text(strip=True)
                    # 後続の段落を集める
                    paras = []
                    i += 1
                    while i < len(tags):
                        next_tag = tags[i]
                        if next_tag.name == 'p' and next_tag.find('a') and \
                           next_tag.find('a').get('href', '').startswith('http'):
                            break  # 次の記事
                        if next_tag.name == 'p':
                            text = next_tag.get_text(strip=True)
                            if text:
                                paras.append(text)
                        i += 1
                    body = ' '.join(paras)
                    articles.append({
                        'id': current_id,
                        'issue': issue_info['id'],
                        'cat': score_category(title + ' ' + body),
                        'title': title[:80],  # 80文字以内
                        'summary': trim_text(body, 200),
                        'detail': trim_text(body, 500) if len(body) > 200 else '',
                        'url': url,
                    })
                    current_id += 1
                else:
                    i += 1

    return articles


def main():
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    existing_issue_ids = {i['id'] for i in data['issues']}
    next_id = max((a['id'] for a in data['articles']), default=0) + 1

    service = get_gmail_service()

    results = service.users().messages().list(
        userId='me',
        q=f'from:{SENDER} subject:週刊Life is beautiful newer_than:8d',
        maxResults=5,
    ).execute()

    messages = results.get('messages', [])
    if not messages:
        print('新しいメルマガはありません。')
        return

    new_articles = []
    new_issues = []

    for msg in messages:
        subject, html_body = get_message_body(service, msg['id'])
        issue_info = parse_issue_info(subject)
        if not issue_info:
            print(f'日付解析失敗: {subject}')
            continue
        if issue_info['id'] in existing_issue_ids:
            print(f'処理済みのためスキップ: {issue_info["label"]}')
            continue
        if not html_body:
            print(f'本文なし: {issue_info["label"]}')
            continue

        print(f'処理中: {issue_info["label"]}')
        articles = extract_articles(html_body, issue_info, next_id)
        new_articles.extend(articles)
        new_issues.append(issue_info)
        next_id += len(articles)
        existing_issue_ids.add(issue_info['id'])
        print(f'  → {len(articles)}件の記事を抽出')

    if not new_articles:
        print('追加する記事はありません。')
        return

    data['articles'] = new_articles + data['articles']
    data['issues'] = sorted(
        new_issues + data['issues'],
        key=lambda x: x['date'],
        reverse=True,
    )
    data['lastUpdated'] = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f'完了: +{len(new_articles)}件の記事、+{len(new_issues)}号を追加')


if __name__ == '__main__':
    main()
