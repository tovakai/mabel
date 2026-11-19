//! `?`: the keys of the channel (or app) on screen, then the ones that work everywhere.
//! Kept in step with HELP in tui/mabel_tui/app.py; a channel lists only what this build has.

pub type Keys = &'static [(&'static str, &'static str)];

pub const EVERYWHERE: Keys = &[
    ("alt+1–8 / F1–F8", "switch channel (again on COMMS, CREATIVE or SYS: their next app)"),
    ("h j k l", "move between sections (the lit border)"),
    ("i / a / enter", "go into the lit section"),
    ("esc", "back out to the sections"),
    ("inside: h j k l", "like the arrow keys, where the section doesn't use the letter"),
    ("ctrl+z", "drop to a plain shell (exit comes back)"),
    ("ctrl+p", "play / pause music"),
    ("ctrl+t", "colour tuner (for screens that show colours their own way)"),
    ("?", "this list"),
    ("ctrl+q", "power off"),
];

/// A channel's own keys by its id. Empty until the channel is ported.
pub fn channel(id: &str) -> Keys {
    match id {
        "comms" => &[
            ("enter", "send"),
            ("ctrl+n", "new chat"),
            ("ctrl+s", "show/hide the chat list"),
            ("esc, k, then i", "into the conversation to scroll it (j k, PgUp PgDn, g G)"),
            ("PgUp / PgDn", "scroll the conversation from the prompt"),
            ("y / n", "allow once / deny a held action (keys land in the conversation)"),
            ("ctrl+s, then h → list", "open another chat (the list hides on the deck)"),
            ("F1 again", "COMMS: MABEL ⇄ INBOX"),
        ],
        "sys-status" | "sys-network" | "sys-bluetooth" | "sys-audio" | "sys-time" => &[
            ("F8 / alt+8 again", "next app (the deck: STATUS · NETWORK · BLUETOOTH · AUDIO · TIME)"),
            ("j k  enter", "move · do the line (connect, pair, switch, change)"),
            ("h l  or  - +", "adjust a value (volume, brightness)"),
            ("x", "forget a Wi-Fi network · remove a Bluetooth device"),
            ("r", "refresh / rescan"),
            ("pairing", "a keyboard's PIN shows at the bottom: type it on the keyboard, then Enter"),
            ("STATUS", "restart or shut down MABEL-OS; on the deck also suspend, reboot, power off (enter twice)"),
        ],
        "audio" => &[
            ("/", "search"),
            ("↑↓ enter", "open album/artist, or play from that song"),
            ("a / n", "append / play next (inside the library; in nav mode a goes in)"),
            ("backspace", "back"),
            ("p", "playlists"),
            ("s", "add selection to a playlist"),
            ("S", "save the queue as a playlist"),
            ("d", "remove from the open playlist"),
            ("z", "shuffle on/off (shuffles the rest of the queue)"),
            ("R", "a random mix from the library"),
            ("space", "pause"),
            ("[ ]  or  < >", "previous / next song"),
            ("← →", "seek 10 s"),
            ("+ -", "volume"),
            ("DECK: j k enter", "jump to a song in the queue"),
        ],
        "life" => &[
            ("r", "refresh now (it also does every minute)"),
            ("v / p", "open her last dream / paper note in VAULT (once VAULT is ported)"),
            ("inside: j k  PgUp PgDn  g G", "scroll the panel"),
        ],
        "inbox" => &[
            ("F1 again", "COMMS: MABEL ⇄ INBOX"),
            ("CHATS: j k enter", "move · open a chat"),
            ("/  u  , .", "filter chats · unread only · step through networks"),
            ("r", "refresh the chats"),
            ("CONVERSATION: j k", "pick a message"),
            ("i / tab / enter", "write (enter sends, esc stops)"),
            ("r  e  +  m", "reply to it · edit yours · react · mark the chat unread"),
            ("h / esc", "back to the chats"),
        ],
        _ => &[],
    }
}
