# ===============================
# i18n — lightweight JP/EN toggle
# ===============================
# UI string translations (Japanese). User-generated content (usernames,
# reviews, chat messages) is NEVER translated — it stays as written.

_translations = {
    # ---- Navbar ----
    "New": "新着",
    "Upcoming": "放送予定",
    "Popular": "人気",
    "Trending": "急上昇",
    "Critics": "批評家",
    "Categories": "カテゴリ",
    "Airing Now": "放送中",
    "Browse All (A–Z)": "全作品 (A–Z)",
    "Genres": "ジャンル",
    "Underrated": "過小評価",
    "Characters": "キャラクター",
    "Reviews": "レビュー",
    "Threads": "スレッド",
    "New to Anime": "アニメ初めて",
    "Settings": "設定",
    "History": "履歴",
    "Anime List": "アニメリスト",
    "Log Out": "ログアウト",
    "Login": "ログイン",
    "Sign Up": "新規登録",
    "Your profile": "プロフィール",

    # ---- Search ----
    "Search anime...": "アニメを検索...",
    "Search": "検索",
    "No results found": "結果が見つかりません",
    "Searching...": "検索中...",

    # ---- Home / browse ----
    "Home": "ホーム",
    "Browse": "閲覧",
    "View All": "すべて見る",
    "Episodes": "エピソード",
    "Seasons": "シーズン",
    "Movies": "映画",
    "Members": "メンバー",

    # ---- Anime page ----
    "Community Reviews": "コミュニティレビュー",
    "Go to Reviews": "レビューへ",
    "Write a Review": "レビューを書く",
    "Your rating:": "あなたの評価:",
    "Tap a star to choose your rating": "星をタップして評価を選んでください",
    "Share your thoughts about this anime...": "このアニメについて感想を書こう...",
    "Post Review": "レビューを投稿",
    "Posting as": "投稿者:",
    "Log in to post a review.": "レビューを投稿するにはログインしてください。",
    "You already reviewed this anime": "このアニメはレビュー済みです",
    "Delete & Re-review": "削除して再レビュー",
    "Reviews cannot be edited after posting": "投稿後のレビューは編集できません",
    "No reviews yet — be the first to rate": "まだレビューはありません — 最初に評価しましょう",
    "Season": "シーズン",
    "Episodes of": "エピソード",
    "Summary": "あらすじ",
    "Information": "情報",
    "Studio": "制作会社",
    "Type": "種類",
    "Status": "状態",
    "Source": "原作",
    "Duration": "尺",
    "Total Episodes": "エピソード数",
    "Streaming": "配信",
    "Watch": "視聴",
    "Sub": "字幕",
    "Dub": "吹替",
    "You May Also Like": "こちらもおすすめ",
    "View Episodes": "エピソードを見る",
    "Rate": "評価",
    "Rating": "評価",
    "Ep": "話",
    "Years": "年",
    "Genres:": "ジャンル:",
    "Recommendations": "おすすめ",

    # ---- Reviews page ----
    "How Reviews & XP Work": "レビューとXPの仕組み",
    "Anime Reviews": "アニメレビュー",
    "Episode Reviews": "エピソードレビュー",
    "Rating": "評価",
    "XP & Votes": "XPと投票",
    "Ranks": "ランク",
    "Higher ranked reviews show first": "上位ランクのレビューが先に表示されます",
    "Your review": "あなたのレビュー",
    "Delete": "削除",
    "Share": "共有",

    # ---- Episode page ----
    "Rate This Episode": "このエピソードを評価",
    "Tap a star to rate out of 5": "1〜5の星をタップして評価",
    "Rate Episode": "評価する",
    "Episode Reviews": "エピソードレビュー",
    "Write a short review of this episode": "このエピソードの短いレビューを書こう",
    "Log in to rate this episode": "このエピソードを評価するにはログインしてください",
    "You already reviewed this episode": "このエピソードはレビュー済みです",
    "Back to": "戻る",

    # ---- Auth ----
    "Welcome back": "おかえりなさい",
    "Log in to your account": "アカウントにログイン",
    "Username": "ユーザー名",
    "Password": "パスワード",
    "Email": "メールアドレス",
    "Create Account": "アカウント作成",
    "Forgot password?": "パスワードをお忘れですか？",

    # ---- Threads ----
    "Messages": "メッセージ",
    "Guilds": "ギルド",
    "Site": "サイト",
    "Discover": "発見",
    "Create Guild": "ギルド作成",
    "Members": "メンバー",
    "Invite": "招待",
    "Copy Link": "リンクをコピー",
    "Send to a friend": "友達に送る",

    # ---- Misc / footer ----
    "The place where every anime has its own community.": "すべてのアニメにコミュニティがある場所。",
    "Discover. Rate. Discuss. Share your passion with anime fans around the world.": "発見。評価。議論。世界中のアニメファンと情熱を共有しよう。",
    "All rights reserved": "全著作権所有",
    "You haven't reviewed this anime yet. Be the first!": "まだこのアニメをレビューしていません。最初にレビューしましょう！",
}


def t(text):
    """Translate a UI string to Japanese if the current lang is 'ja'."""
    if get_language() == "ja":
        return _translations.get(text, text)
    return text


def ja(text, japanese):
    """Return japanese if lang is ja, else the english text."""
    if get_language() == "ja":
        return japanese
    return text


def get_language():
    """Read the current language from the request cookie."""
    from flask import request, has_request_context
    if has_request_context():
        return request.cookies.get("lang", "en")
    return "en"