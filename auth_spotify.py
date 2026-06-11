from config import SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI
from spotipy.oauth2 import SpotifyOAuth
import spotipy

def main():
    print("Инициализация Spotify Auth...")
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope="user-library-read user-library-modify",
        open_browser=False
    )
    sp = spotipy.Spotify(auth_manager=auth_manager)
    
    # Делаем тестовый запрос, чтобы триггернуть авторизацию
    user = sp.current_user()
    print(f"\n✅ Успешно авторизовано как: {user['id']}")

if __name__ == "__main__":
    main()
