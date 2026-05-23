#!/usr/bin/env python3
"""
週刊Life is beautiful メルマガ自動取得・分類スクリプト
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
from anthropic import Anthropic
from bs4 import BeautifulSoup

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
DATA_PATH = 'newsletter/data.json'
SENDER = 'mailmag@mag2premium.com'

CATEGORY_GUIDE = """
カテゴリの選択肢（1つだけ選ぶ）:
- ai-llm    : AI・LLM・言語モデル・Claude・ChatGPT・エージェント・Anthropic・OpenAI
- software  : ソフトウェア開発・プログラミング・コーディング・アーキテクチャ・バイブコーディング
- business  : ビジネス・企業・スタートアップ・経営・M&A・採用・人事
- economy   : 経済・雇用・投資・市場・社会構造・労働問題
- infra     : データセンター・半導体・電力・インフラ・ハードウェア・GPU・CPU・エネルギー
- robot     : ロボット・ヒューマノイド・自動運転・フィジカルAI・ドローン
- science   : 科学・医療・バイオテク・研究・長寿・創薬・ゲノム
- policy    : 規制・政策・法律・政府・安全保障・地政学
- personal  : 個人体験・旅行・コラム・ざっくばらん・音楽・スポーツ
- qa        : 質問コーナー・Q&A（複数の質問をまとめて1記事として扱う）
"""


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
    """件名から号IDと日付を取得する"""
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


def extract_articles_with_claude(html_body, issue_info, next_id):
    """Claude APIを使ってメルマガからトピックを抽出・分類する"""
    soup = BeautifulSoup(html_body, 'html.parser')
    # フッター以降を除去
    for tag in soup.select('table#mag2-pay-magazine-footer, hr'):
        tag.decompose()
    text = soup.get_text(separator='\n', strip=True)[:9000]

    client = Anthropic()
    prompt = f"""以下は「週刊Life is beautiful {issue_info['label']}」のメルマガ全文です。

{text}

このメルマガに含まれるトピック・記事を全て抽出し、以下のJSON形式のみで出力してください。
余分な説明文やコードブロック記号は不要です。

{{
  "articles": [
    {{
      "title": "記事タイトル（日本語・50文字以内・具体的に）",
      "cat": "カテゴリ",
      "summary": "2〜3文の日本語要約（具体的な数字・固有名詞を含めて）",
      "detail": "さらに詳しい補足説明（あれば・なければ空文字）",
      "url": "言及されている元記事のURL（なければ空文字）"
    }}
  ]
}}

{CATEGORY_GUIDE}

抽出ルール:
- 「今週のざっくばらん」セクション：各話題を personal カテゴリで個別に抽出
- 「私の目に留まった記事」セクション：各リンク記事を個別に抽出
- 「質問コーナー」セクション：qa カテゴリで1件としてまとめる（個別Q&Aは不要）
- summaryには固有名詞・数字・具体的な主張を必ず含める
- urlはメルマガ本文に明示されているリンクのみ記載
"""

    response = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=4096,
        messages=[{'role': 'user', 'content': prompt}],
    )

    content = response.content[0].text.strip()
    # JSON部分だけ取り出す
    m = re.search(r'\{[\s\S]*\}', content)
    if not m:
        raise ValueError(f"JSONが見つかりません: {content[:200]}")

    data = json.loads(m.group())
    articles = []
    for i, a in enumerate(data.get('articles', [])):
        articles.append({
            'id': next_id + i,
            'issue': issue_info['id'],
            'cat': a.get('cat', 'personal'),
            'title': a.get('title', ''),
            'summary': a.get('summary', ''),
            'detail': a.get('detail', ''),
            'url': a.get('url', ''),
        })
    return articles


def main():
    # 既存データ読み込み
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    existing_issue_ids = {i['id'] for i in data['issues']}
    next_id = max((a['id'] for a in data['articles']), default=0) + 1

    # Gmail接続
    service = get_gmail_service()

    # 直近8日以内の新着メルマガを検索
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
        try:
            articles = extract_articles_with_claude(html_body, issue_info, next_id)
            new_articles.extend(articles)
            new_issues.append(issue_info)
            next_id += len(articles)
            existing_issue_ids.add(issue_info['id'])
            print(f'  → {len(articles)}件の記事を抽出')
        except Exception as e:
            print(f'  エラー: {e}')

    if not new_articles:
        print('追加する記事はありません。')
        return

    # データ更新（新しい号を先頭に）
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
