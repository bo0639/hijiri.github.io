# プロスピA 獲得リスト

プロスピA で欲しい選手を記録し、どのガチャ・イベントから回すと効率がよいかを
判断するための個人用ツール（ベース版）。

公開URL: `https://bo0639.github.io/hijiri.github.io/prospia/`

## できること

- **選手一覧**: 選手画像（またはチーム色＋背番号）・所属チーム・ポジション・投打・優先度★と、
  シリーズごとの所持状況（◎狙い中／○欲しい／✓獲得済／−見送り）をカードで一覧表示
- **シリーズ管理**: 1人の選手に「2026 シリーズ1」「シリーズ2」「タイムスリップ」などを複数登録し、
  シリーズごとに能力・ランク・入手先・期間・画像を持てる
- **シリーズ比較**: 詳細画面で能力をシリーズ横並びの表で比較（基準との差分・最高値をハイライト）
- **獲得プラン**: 未獲得のシリーズを入手先（ガチャ・イベント名）ごとにまとめ、
  ★優先度の合計と期限で並べる。そのまま「獲得」ボタンで獲得済にできる
- 入手期限が3日以内のシリーズをアラート表示
- 検索・絞り込み（獲得状況／投手・野手／球団／シリーズ）・並び替え
- JSONエクスポート／インポート、GitHub同期（`prospia/data.json`、他ツールと同じ方式）

## データ構造

`data.json` は選手の配列。シリーズ（カード）は選手の `cards` に入る。

```json
{
  "id": "…", "name": "山田 太郎", "kana": "やまだ たろう",
  "team": "tigers", "number": "1", "role": "fielder",
  "position": "遊撃手", "hand": "右投左打", "priority": 3,
  "image": "https://… または data:image/jpeg;base64,…", "memo": "",
  "cards": [
    {
      "id": "…", "series": "シリーズ2", "year": "2026", "rank": "S", "status": "target",
      "stats": {"meet": 88, "power": 72, "speed": 90, "arm": 81, "field": 88, "catch": 80},
      "pitches": "", "specials": "盗塁◎",
      "method": "スカウト（ガチャ）", "source": "シリーズ2 第1弾",
      "start": "2026-10-15", "end": "2026-10-22", "image": "", "note": ""
    }
  ],
  "updated_at": "…"
}
```

- `role` が `pitcher` のときの `stats` は `velo`（球速km/h）・`control`・`stamina`
- `status`: `target`（狙い中）／`want`（欲しい）／`owned`（獲得済）／`skip`（見送り）
- `team`: `tigers` `giants` `baystars` `carp` `swallows` `dragons` `hawks` `fighters`
  `marines` `eagles` `buffaloes` `lions` `ob` `other`

## 画像について

- 画像URLを貼るか、端末の画像（スクショなど）を選ぶと 240×320 のJPEGに縮小して保存する
- 端末から取り込んだ画像はデータ本体に入るため、GitHub同期を使うと公開リポジトリの
  `data.json` に含まれる点に注意（気になる場合はURL指定を使う）
