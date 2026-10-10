//! Whether a closed way is a footprint or still a line.
//!
//! The same order as `localosm._is_area_way`. `area=yes` and `area=no` win.
//! A station or a yard is an area even though `railway=*` is otherwise a
//! centre line. A highway, a barrier, a coastline, a pier, or a linear
//! waterway stays a line. Every other closed way is filled.

const RAIL_GROUNDS: &[&str] = &["station", "halt", "platform", "yard"];
const LINE_KEYS: &[&str] = &["highway", "railway", "barrier"];
const PIERS: &[&str] = &["pier", "breakwater", "groyne"];
const LINE_WATERWAYS: &[&str] = &[
    "river",
    "stream",
    "canal",
    "ditch",
    "drain",
    "pressurised",
    "tidal_channel",
    "fairway",
    "dam",
    "weir",
    "lock_gate",
];

fn skip(text: &str, i: &mut usize) {
    let bytes = text.as_bytes();
    while *i < bytes.len() && bytes[*i].is_ascii_whitespace() {
        *i += 1;
    }
}

fn parse_string(text: &str, i: &mut usize) -> Option<String> {
    let bytes = text.as_bytes();
    if *i >= bytes.len() || bytes[*i] != b'"' {
        return None;
    }
    *i += 1;
    let mut out = String::new();
    while *i < text.len() {
        let c = text[*i..].chars().next()?;
        *i += c.len_utf8();
        if c == '"' {
            return Some(out);
        }
        if c != '\\' {
            out.push(c);
            continue;
        }
        let e = text[*i..].chars().next()?;
        *i += e.len_utf8();
        match e {
            '"' | '\\' | '/' => out.push(e),
            'b' => out.push('\u{0008}'),
            'f' => out.push('\u{000c}'),
            'n' => out.push('\n'),
            'r' => out.push('\r'),
            't' => out.push('\t'),
            'u' => {
                if *i + 4 > text.len() {
                    return None;
                }
                let hex = &text[*i..*i + 4];
                *i += 4;
                let code = u32::from_str_radix(hex, 16).ok()?;
                out.push(char::from_u32(code)?);
            }
            _ => return None,
        }
    }
    None
}

/// Flat JSON object of strings, as `json.dumps` writes a tag dict.
pub fn tag_pairs(raw: &str) -> Option<Vec<(String, String)>> {
    tags_of(raw)
}

/// Flat JSON object of strings, as `json.dumps` writes a tag dict.
fn tags_of(raw: &str) -> Option<Vec<(String, String)>> {
    let text = raw.trim();
    if text.len() < 2 || !text.starts_with('{') || !text.ends_with('}') {
        return None;
    }
    let mut i = 1;
    let mut pairs = Vec::new();
    skip(text, &mut i);
    if text.as_bytes().get(i) == Some(&b'}') {
        return Some(pairs);
    }
    loop {
        let key = parse_string(text, &mut i)?;
        skip(text, &mut i);
        if text.as_bytes().get(i) != Some(&b':') {
            return None;
        }
        i += 1;
        skip(text, &mut i);
        let value = parse_string(text, &mut i)?;
        pairs.push((key, value));
        skip(text, &mut i);
        match text.as_bytes().get(i) {
            Some(&b'}') => return Some(pairs),
            Some(&b',') => {
                i += 1;
                skip(text, &mut i);
            }
            _ => return None,
        }
    }
}

fn get<'a>(pairs: &'a [(String, String)], key: &str) -> Option<&'a str> {
    pairs
        .iter()
        .find(|(k, _)| k == key)
        .map(|(_, value)| value.as_str())
}

fn has(pairs: &[(String, String)], key: &str) -> bool {
    pairs.iter().any(|(k, _)| k == key)
}

/// True when a closed way is filled. An open way is never an area.
pub fn is_area(tags_json: &[u8], closed: bool) -> bool {
    if !closed || tags_json.is_empty() {
        return false;
    }
    let text = match std::str::from_utf8(tags_json) {
        Ok(text) => text,
        Err(_) => return false,
    };
    let pairs = match tags_of(text) {
        Some(pairs) if !pairs.is_empty() => pairs,
        _ => return false,
    };
    if get(&pairs, "area") == Some("yes") {
        return true;
    }
    if get(&pairs, "area") == Some("no") {
        return false;
    }
    if let Some(railway) = get(&pairs, "railway") {
        if RAIL_GROUNDS.iter().any(|item| *item == railway) {
            return true;
        }
    }
    if get(&pairs, "natural") == Some("coastline") {
        return false;
    }
    if LINE_KEYS.iter().any(|key| has(&pairs, key)) {
        return false;
    }
    if let Some(made) = get(&pairs, "man_made") {
        if PIERS.iter().any(|item| *item == made) {
            return false;
        }
    }
    if let Some(waterway) = get(&pairs, "waterway") {
        if LINE_WATERWAYS.iter().any(|item| *item == waterway) {
            return false;
        }
    }
    true
}
