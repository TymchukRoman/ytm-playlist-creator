"""Built-in sample library for demo mode (`YTSORTER_DEMO=1`): ~300 songs and a keyword-based rule.

Nothing here talks to YouTube or Claude. The tag of each song lives in `TAGS`, not on `Song`.
"""
import time
import zlib

from .fakes import FakeClassifier
from .models import Decision, Song, Verdict

BATCH_DELAY = 0.3  # seconds of fake "thinking" per batch, so the progress bar is visible

# One song per line: title | artists (comma separated) | album. Real, well-known tracks.
_RAW: dict[str, str] = {
    "anime-opening": """
Gurenge | LiSA | Gurenge
Homura | LiSA | Homura
Unravel | TK from Ling tosite sigure | Unravel
Again | YUI | Again
Blue Bird | Ikimono-gakari | Blue Bird
Silhouette | KANA-BOON | Silhouette
Guren no Yumiya | Linked Horizon | Guren no Yumiya
Shinzou wo Sasageyo! | Linked Horizon | Shinzou wo Sasageyo!
Peace Sign | Kenshi Yonezu | Peace Sign
Departure! | Masatoshi Ono | Departure!
We Are! | Hiroshi Kitadani | We Are!
Haruka Kanata | ASIAN KUNG-FU GENERATION | Haruka Kanata
Rewrite | ASIAN KUNG-FU GENERATION | Rewrite
Cruel Angel's Thesis | Yoko Takahashi | A Cruel Angel's Thesis
Tank! | Seatbelts | Cowboy Bebop OST 1
Cha-La Head-Cha-La | Hironobu Kageyama | Dragon Ball Z
Fighting Gold | Coda | Fighting Gold
Kaikai Kitan | Eve | Kaikai Kitan
Zenzenzense | RADWIMPS | Zenzenzense
Racing into the Night | YOASOBI | Racing into the Night
Dark Seeks Light | Hiroyuki Sawano | Dark Seeks Light
Sign | FLOW | Sign
Go!!! | FLOW | Go!!!
Uninstall | Ling tosite sigure | Uninstall
The Hero!! | JAM Project | The Hero!!
Butter-Fly | Koji Wada | Butter-Fly
Crossing Field | LiSA | Crossing Field
Idol | YOASOBI | Idol
Yuusha | YOASOBI | Yuusha
Kick Back | Kenshi Yonezu | Kick Back
Rising Hope | LiSA | Rising Hope
Ignite | Eir Aoi | Ignite
Sakura Mirage | Hiroyuki Sawano | Sakura Mirage
Mixed Nuts | Official HIGE DANdism | Mixed Nuts
Inferno | Mrs. GREEN APPLE | Inferno
Colors | FLOW | Colors
Brave Shine | Aimer | Brave Shine
Zankyou Sanka | Aimer | Zankyou Sanka
Kizuna no Kiseki | MAN WITH A MISSION | Kizuna no Kiseki
Vivid Vice | Who-ya Extended | Vivid Vice
Ao no Sumika | Tatsuya Kitani | Ao no Sumika
Seishun Complex | Kessoku Band | Seishun Complex
Imagination | SPYAIR | Imagination
Hikari Are | Burnout Syndromes | Hikari Are
Good Morning World! | Burnout Syndromes | Good Morning World!
Mukanjyo | Survive Said The Prophet | Mukanjyo
Bloody Stream | Coda | Bloody Stream
Great Days | Coda | Great Days
Closer | Joe Inoue | Closer
Rocks | Hound Dog | Rocks
Alumina | Nightmare | Alumina
Snow Fairy | Funkist | Snow Fairy
Hacking to the Gate | Kanako Ito | Hacking to the Gate
Redo | Konomi Suzuki | Redo
Fantastic Dreamer | Machico | Fantastic Dreamer
Lilium | Kumiko Noma | Elfen Lied
Sobakasu | Judy and Mary | Sobakasu
Asphyxia | Cö shu Nie | Asphyxia
Polaris | Blue Encount | Polaris
Odd Future | UVERworld | Odd Future
Heroes | Brian the Sun | Heroes
Hare Hare Yukai | Aya Hirano | Hare Hare Yukai
Paradisus-Paradoxum | MYTH & ROID | Paradisus-Paradoxum
""",
    "anime-other": """
Don't Say Lazy | Sakurakai | Don't Say Lazy
Snow Halation | μ's | Snow Halation
Spirited Away Always With Me | Youmi Kimura | Always With Me
Merry-Go-Round of Life | Joe Hisaishi | Howl's Moving Castle OST
One Summer's Day | Joe Hisaishi | Spirited Away OST
Carrying You | Azumi Inoue | Laputa: Castle in the Sky
Princess Mononoke Theme | Yoshikazu Mera | Princess Mononoke OST
Your Name Date | RADWIMPS | Kimi no Na wa OST
Nandemonaiya | RADWIMPS | Kimi no Na wa OST
Grand Escape | RADWIMPS | Weathering With You OST
Is There Still Anything That Love Can Do? | RADWIMPS | Weathering With You OST
Suzume | RADWIMPS, Toaka | Suzume OST
Violet Evergarden Sincerely | TRUE | Sincerely
Battle Without Honor | Tomoyasu Hotei | Kill Bill OST
Fly Me to the Moon | Claire Littley | Neon Genesis Evangelion
One Last Kiss | Hikaru Utada | One Last Kiss
Beautiful World | Hikaru Utada | Beautiful World
Komm, süsser Tod | Arianne | Neon Genesis Evangelion
Always With Me | Youmi Kimura | Spirited Away
The Path of the Wind | Joe Hisaishi | My Neighbor Totoro OST
My Neighbor Totoro | Azumi Inoue | My Neighbor Totoro
A Town with an Ocean View | Joe Hisaishi | Kiki's Delivery Service OST
Gake no Ue no Ponyo | Fujioka Fujimaki, Nozomi Ohashi | Ponyo
The Sixth Station | Joe Hisaishi | Spirited Away OST
Hikouki Gumo | Yumi Arai | The Wind Rises
Himawari no Yakusoku | Motohiro Hata | Stand by Me Doraemon
Blue | Seatbelts | Cowboy Bebop OST 3
Kaze ni Naru | Ayano Tsuji | The Cat Returns
Sailor Moon Moonlight Legend | DALI | Sailor Moon
""",
    "k-pop": """
Dynamite | BTS | BE
Butter | BTS | Butter
Boy With Luv | BTS, Halsey | Map of the Soul: Persona
Spring Day | BTS | You Never Walk Alone
DNA | BTS | Love Yourself: Her
Fake Love | BTS | Love Yourself: Tear
Blood Sweat & Tears | BTS | Wings
Life Goes On | BTS | BE
How You Like That | BLACKPINK | The Album
Kill This Love | BLACKPINK | Kill This Love
DDU-DU DDU-DU | BLACKPINK | Square Up
Pink Venom | BLACKPINK | Born Pink
Shut Down | BLACKPINK | Born Pink
Lovesick Girls | BLACKPINK | The Album
Ice Cream | BLACKPINK, Selena Gomez | The Album
Love Dive | IVE | Love Dive
Eleven | IVE | Eleven
After LIKE | IVE | After Like
Super Shy | NewJeans | Get Up
Hype Boy | NewJeans | New Jeans
Ditto | NewJeans | OMG
OMG | NewJeans | OMG
Attention | NewJeans | New Jeans
Gee | Girls' Generation | Gee
Into the New World | Girls' Generation | Girls' Generation
Genie | Girls' Generation | Genie
Mr. Mr. | Girls' Generation | Mr.Mr.
Fantastic Baby | BIGBANG | Alive
Bang Bang Bang | BIGBANG | Made
Fxxk It | BIGBANG | Made
Gangnam Style | PSY | Psy 6 (Six Rules), Part 1
Gentleman | PSY | Gentleman
Next Level | aespa | Next Level
Spicy | aespa | My World
Supernova | aespa | Armageddon
Black Mamba | aespa | Savage
Drama | aespa | Drama
Maestro | SEVENTEEN | Seventeenth Heaven
Super | SEVENTEEN | Semicolon
God of Music | SEVENTEEN | Seventeenth Heaven
Very Nice | SEVENTEEN | Boys Be
Cheer Up | TWICE | Page Two
TT | TWICE | Twicecoaster: Lane 1
Fancy | TWICE | Fancy You
Feel Special | TWICE | Feel Special
What is Love? | TWICE | What is Love?
God's Menu | Stray Kids | Go Live
S-Class | Stray Kids | 5-Star
Wolf | EXO | Exodus
Growl | EXO | Growl
Love Shot | EXO | Love Shot
Call Me Baby | EXO | Exodus
Sorry Sorry | Super Junior | Sorry, Sorry
Mamacita | Super Junior | Mamacita
Ring Ding Dong | SHINee | Ring Ding Dong
Lucifer | SHINee | Lucifer
Hot Sauce | NCT DREAM | Hot Sauce
Cherry Bomb | NCT 127 | Cherry Bomb
Fearless | LE SSERAFIM | Fearless
Antifragile | LE SSERAFIM | Antifragile
Unforgiven | LE SSERAFIM, Nile Rodgers | Unforgiven
Given-Taken | ENHYPEN | Border: Day One
Bad Boy | Red Velvet | The Perfect Red Velvet
Psycho | Red Velvet | The ReVe Festival: Finale
Dumb Dumb | Red Velvet | Ice Cream Cake
Rookie | Red Velvet | Rookie
Palette | IU | Palette
Blueming | IU | Love Poem
Celebrity | IU | LILAC
Good Day | IU | Real
Through the Night | IU | Palette
Love wins all | IU | Love wins all
Eight | IU, SUGA | Eight
Don't Stop the Music | Taeyeon | Why
""",
    "rock": """
Bohemian Rhapsody | Queen | A Night at the Opera
Don't Stop Me Now | Queen | Jazz
We Will Rock You | Queen | News of the World
Another One Bites the Dust | Queen | The Game
Stairway to Heaven | Led Zeppelin | Led Zeppelin IV
Whole Lotta Love | Led Zeppelin | Led Zeppelin II
Black Dog | Led Zeppelin | Led Zeppelin IV
Hotel California | Eagles | Hotel California
Smoke on the Water | Deep Purple | Machine Head
Highway to Hell | AC/DC | Highway to Hell
Back in Black | AC/DC | Back in Black
Thunderstruck | AC/DC | The Razors Edge
Paranoid | Black Sabbath | Paranoid
Iron Man | Black Sabbath | Paranoid
Enter Sandman | Metallica | Metallica
Nothing Else Matters | Metallica | Metallica
One | Metallica | And Justice for All
Master of Puppets | Metallica | Master of Puppets
Welcome to the Jungle | Guns N' Roses | Appetite for Destruction
Sweet Child O' Mine | Guns N' Roses | Appetite for Destruction
November Rain | Guns N' Roses | Use Your Illusion I
Smells Like Teen Spirit | Nirvana | Nevermind
Come as You Are | Nirvana | Nevermind
Lithium | Nirvana | Nevermind
Black Hole Sun | Soundgarden | Superunknown
Alive | Pearl Jam | Ten
Even Flow | Pearl Jam | Ten
Everlong | Foo Fighters | The Colour and the Shape
Learn to Fly | Foo Fighters | There Is Nothing Left to Lose
The Pretender | Foo Fighters | Echoes, Silence, Patience & Grace
Under the Bridge | Red Hot Chili Peppers | Blood Sugar Sex Magik
Californication | Red Hot Chili Peppers | Californication
Can't Stop | Red Hot Chili Peppers | By the Way
Basket Case | Green Day | Dookie
American Idiot | Green Day | American Idiot
Boulevard of Broken Dreams | Green Day | American Idiot
Seven Nation Army | The White Stripes | Elephant
Mr. Brightside | The Killers | Hot Fuss
Somebody Told Me | The Killers | Hot Fuss
Last Nite | The Strokes | Is This It
Do I Wanna Know? | Arctic Monkeys | AM
R U Mine? | Arctic Monkeys | AM
I Bet You Look Good on the Dancefloor | Arctic Monkeys | Whatever People Say I Am
Wonderwall | Oasis | (What's the Story) Morning Glory?
Don't Look Back in Anger | Oasis | (What's the Story) Morning Glory?
Creep | Radiohead | Pablo Honey
Paranoid Android | Radiohead | OK Computer
Karma Police | Radiohead | OK Computer
Yellow | Coldplay | Parachutes
Clocks | Coldplay | A Rush of Blood to the Head
In the End | Linkin Park | Hybrid Theory
Numb | Linkin Park | Meteora
Crawling | Linkin Park | Hybrid Theory
Faint | Linkin Park | Meteora
Chop Suey! | System of a Down | Toxicity
Toxicity | System of a Down | Toxicity
Duality | Slipknot | Vol. 3
Wait and Bleed | Slipknot | Slipknot
Du Hast | Rammstein | Sehnsucht
Sonne | Rammstein | Mutter
Cut Here Rocks Mirror | Deftones | White Pony
Sweet Home Alabama | Lynyrd Skynyrd | Second Helping
Free Bird | Lynyrd Skynyrd | Pronounced
Born to Run | Bruce Springsteen | Born to Run
Livin' on a Prayer | Bon Jovi | Slippery When Wet
You Give Love a Bad Name | Bon Jovi | Slippery When Wet
Eye of the Tiger | Survivor | Eye of the Tiger
Don't Stop Believin' | Journey | Escape
Kashmir | Led Zeppelin | Physical Graffiti
Comfortably Numb | Pink Floyd | The Wall
Another Brick in the Wall | Pink Floyd | The Wall
Wish You Were Here | Pink Floyd | Wish You Were Here
Money | Pink Floyd | The Dark Side of the Moon
""",
    "ukrainian": """
Stefania | Kalush Orchestra | Stefania
Shum | Go_A | Shum
Zavzhdy | Okean Elzy | Zavzhdy
Obijmy | Okean Elzy | Obijmy
Vzhe Vsi Pishly Dodomu | Okean Elzy | Vzhe vsi pishly dodomu
Ya Tak Skuchayu | Okean Elzy | Ya tak skuchayu
Tam de nas nema | Okean Elzy | Tam de nas nema
Ne pytai | Okean Elzy | Supersymetriya
Wild Dances | Ruslana | Wild Dances
Kolyskova | Jamala | Kolyskova
1944 | Jamala | Thousand Pieces
Mama | KAZKA | Nirvana
Plakala | KAZKA | Nirvana
Svyatyi Vechir | KAZKA | Nirvana
Oi u luzi chervona kalyna | The Hardkiss | Kalyna
Rock n Roll | The Hardkiss | Zalizna Lyubov
Helpless | The Hardkiss | Zalizna Lyubov
Fortetsya Bakhmut | Antytila | Fortetsya Bakhmut
Chervona Ruta | Sofia Rotaru | Chervona Ruta
Vodohrai | Sofia Rotaru | Vodohrai
Lyudy yak lyudy | Skryabin | Lyudy yak lyudy
Shchedryk | Ukrainian National Chorus | Carol of the Bells
Vstavay | Okean Elzy | Vstavay
Vsi Svoi | Onuka | Vsi Svoi
Vidlik | Onuka | Vidlik
Pushka | Alyona Alyona | Pushka
Khali-Gali | Boombox | Khali-Gali
I Believe U | Jamala | Thousand Pieces
Cry | KAZKA | Nirvana
Rozy | Dakh Daughters | Freak Cabaret
Solovey | Go_A | Solovey
Show Me Your Love | Tina Karol | Show Me Your Love
Dancing Lasha Tumbai | Verka Serduchka | Dancing Lasha Tumbai
Lavanda | Sofia Rotaru | Lavanda
Nich yaka misyachna | Ukrainian Folk Choir | Ukrainian Folk Songs
Plyve kacha | Ukrainian Folk Choir | Ukrainian Folk Songs
Hei, sokoly | Ukrainian Folk Choir | Ukrainian Folk Songs
Ya ne zdamsya bez boyu | Antytila | Ya ne zdamsya bez boyu
Zhyvy | Svyatoslav Vakarchuk | Zhyvy
Kolyskova | Mashu | Kolyskova
""",
    "other": """
Blinding Lights | The Weeknd | After Hours
Shape of You | Ed Sheeran | Divide
Levitating | Dua Lipa | Future Nostalgia
Bad Guy | Billie Eilish | When We All Fall Asleep, Where Do We Go?
Happier Than Ever | Billie Eilish | Happier Than Ever
As It Was | Harry Styles | Harry's House
Watermelon Sugar | Harry Styles | Fine Line
Someone Like You | Adele | 21
Rolling in the Deep | Adele | 21
Get Lucky | Daft Punk | Random Access Memories
One More Time | Daft Punk | Discovery
Around the World | Daft Punk | Homework
Take Five | Dave Brubeck | Time Out
So What | Miles Davis | Kind of Blue
Clair de Lune | Claude Debussy | Suite Bergamasque
Gymnopédie No. 1 | Erik Satie | Gymnopédies
Moonlight Sonata | Ludwig van Beethoven | Piano Sonatas
Billie Jean | Michael Jackson | Thriller
Thriller | Michael Jackson | Thriller
Uptown Funk | Mark Ronson, Bruno Mars | Uptown Special
Lose Yourself | Eminem | 8 Mile
Stan | Eminem | The Marshall Mathers LP
Redbone | Childish Gambino | Awaken, My Love!
Electric Feel | MGMT | Oracular Spectacular
Midnight City | M83 | Hurry Up, We're Dreaming
Dreams | Fleetwood Mac | Rumours
Africa | Toto | Toto IV
Take On Me | a-ha | Hunting High and Low
Weightless | Marconi Union | Weightless
Fur Elise | Ludwig van Beethoven | Bagatelles
""",
}

# Which description keywords map to which tags, and a human label for reasons.
_LABEL = {
    "anime-opening": "an anime opening theme",
    "anime-other": "an anime song, but not an opening",
    "k-pop": "a K-pop track",
    "rock": "a rock song",
    "ukrainian": "a Ukrainian song",
    "other": "something else",
}


def _build() -> tuple[list[Song], dict[str, str]]:
    songs: list[Song] = []
    tags: dict[str, str] = {}
    for tag, block in _RAW.items():
        for line in block.strip().splitlines():
            title, artists, album = (part.strip() for part in line.split(" | "))
            video_id = f"demo{len(songs):04d}"
            minutes, seconds = 2 + zlib.crc32(video_id.encode()) % 4, zlib.crc32(video_id.encode()[::-1]) % 60
            songs.append(Song(video_id, title, [a.strip() for a in artists.split(",")], album,
                              f"{minutes}:{seconds:02d}"))
            tags[video_id] = tag
    return songs, tags


SAMPLE_SONGS, TAGS = _build()

# A deterministic bucket per song, so the same song always gets the same "unsure".
def _bucket(video_id: str, modulo: int) -> bool:
    return zlib.crc32(video_id.encode()) % modulo == 0


def _wanted(description: str) -> tuple[set[str], set[str]]:
    """(tags the description asks for, borderline tags it might also mean)."""
    d = description.lower()
    wanted: set[str] = set()
    borderline: set[str] = set()
    if "opening" in d:
        wanted.add("anime-opening")
        borderline.add("anime-other")
    elif "anime" in d:
        wanted.update({"anime-opening", "anime-other"})
    if any(k in d for k in ("k-pop", "kpop", "k pop", "korean")):
        wanted.add("k-pop")
    if "rock" in d:
        wanted.add("rock")
    if any(k in d for k in ("ukrainian", "україн", "украин")):
        wanted.add("ukrainian")
    return wanted, borderline


def demo_rule(description: str, song: Song) -> tuple[Verdict, str]:
    """Pick a verdict from the description's keywords and the song's built-in tag.

    About 1 in 20 matching songs and 1 in 4 borderline ones (anime songs that aren't openings,
    when openings were asked for) come back `unsure`, always the same ones.
    """
    wanted, borderline = _wanted(description)
    tag = TAGS.get(song.video_id)
    if not wanted:  # no keyword we know: plain word match, like the default fake
        haystack = f"{song.title} {' '.join(song.artists)}".lower()
        for word in description.lower().split():
            if word in haystack:
                return "include", f"matched '{word}'"
        return "exclude", "no match"
    if tag in wanted:
        if _bucket(song.video_id, 20):
            return "unsure", f"Could be {_LABEL[tag]}, but I'm not certain"
        return "include", f"It's {_LABEL[tag]}"
    if tag in borderline:
        if _bucket(song.video_id, 4):
            return "unsure", "An anime song, but I can't tell if it was an opening"
        return "exclude", f"It's {_LABEL[tag]}"
    return "exclude", f"It's {_LABEL.get(tag, 'something else')}"


class DemoClassifier(FakeClassifier):
    """FakeClassifier with demo_rule that pauses briefly per batch so progress is visible."""

    def __init__(self):
        super().__init__(rules=demo_rule)

    def classify_batch(self, description: str, songs: list[Song]) -> list[Decision]:
        if BATCH_DELAY:
            time.sleep(BATCH_DELAY)
        return super().classify_batch(description, songs)
