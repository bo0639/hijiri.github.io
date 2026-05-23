#!/usr/bin/env python3
"""
Gmail認証のセットアップスクリプト（最初に1回だけ実行する）

使い方:
  pip install google-auth-oauthlib
  python scripts/setup_gmail_auth.py

実行するとブラウザが開くので、Googleアカウントでログインして許可する。
完了すると gmail_token.json が生成されるので、その中身を
GitHub SecretsのGMAIL_TOKENに貼り付ける。
"""
import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

def main():
    print('=== Gmail認証セットアップ ===')
    print()
    print('1. このスクリプトを実行するには先に credentials.json が必要です。')
    print('   Google Cloud Console (https://console.cloud.google.com/) で')
    print('   「APIとサービス」→「認証情報」→「OAuthクライアントID作成」')
    print('   （アプリケーションの種類：デスクトップアプリ）を行い、')
    print('   ダウンロードしたJSONを credentials.json としてこのフォルダに置いてください。')
    print()
    input('準備ができたらEnterを押してください...')

    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    creds = flow.run_local_server(port=0)

    token_data = {
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': list(creds.scopes),
    }

    with open('gmail_token.json', 'w') as f:
        json.dump(token_data, f, indent=2)

    print()
    print('✅ gmail_token.json を生成しました。')
    print()
    print('次のステップ:')
    print('  github.com のリポジトリページで')
    print('  Settings → Secrets and variables → Actions → New repository secret')
    print('  Name: GMAIL_TOKEN')
    print('  Value: gmail_token.json の中身（テキスト全体）を貼り付ける')
    print()
    print('⚠️  gmail_token.json と credentials.json は絶対にGitHubにアップロードしないでください。')

if __name__ == '__main__':
    main()
