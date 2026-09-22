//! Notices for what is waiting on the person: agent requests, memory
//! suggestions, and Review. Read from flanner's own `/nav/waiting`.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::thread;
use std::time::Duration;

use serde_json::Value;
use tauri::AppHandle;

const EVERY: Duration = Duration::from_secs(60);

/// (count name, one, many): the words for a count that rose.
const KINDS: [(&str, &str, &str); 3] = [
    (
        "requests",
        "agent request waits for you",
        "agent requests wait for you",
    ),
    (
        "memories",
        "memory suggestion waits for approval",
        "memory suggestions wait for approval",
    ),
    ("review", "item waits in Review", "items wait in Review"),
];

/// Poll once a minute. The first answer only sets the baseline, so
/// starting the app does not announce everything that was already there.
pub fn watch(app: AppHandle, port: u16) {
    thread::spawn(move || {
        let mut before: Option<Value> = None;
        loop {
            if let Some(now) = fetch(port) {
                if let Some(old) = &before {
                    for message in rises(old, &now) {
                        crate::notify(&app, "Flanner", &message);
                    }
                }
                crate::set_waiting(&app, total(&now));
                before = Some(now);
            }
            thread::sleep(EVERY);
        }
    });
}

/// One short GET, with nothing but the standard library: the answer is a
/// few bytes of JSON from this machine.
fn fetch(port: u16) -> Option<Value> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2)).ok()?;
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .ok()?;
    let request =
        format!("GET /nav/waiting HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    stream.write_all(request.as_bytes()).ok()?;
    let mut reply = String::new();
    stream.read_to_string(&mut reply).ok()?;
    let (head, body) = reply.split_once("\r\n\r\n")?;
    if !head.starts_with("HTTP/1.1 200") {
        return None;
    }
    serde_json::from_str(body).ok()
}

fn count(counts: &Value, name: &str) -> u64 {
    counts[name].as_u64().unwrap_or(0)
}

fn total(counts: &Value) -> u64 {
    KINDS.iter().map(|(name, _, _)| count(counts, name)).sum()
}

/// A sentence for each count that went up.
fn rises(before: &Value, now: &Value) -> Vec<String> {
    KINDS
        .iter()
        .filter(|(name, _, _)| count(now, name) > count(before, name))
        .map(|(name, one, many)| {
            let n = count(now, name);
            format!("{n} {}", if n == 1 { one } else { many })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::{rises, total};
    use serde_json::json;

    #[test]
    fn only_a_rise_is_announced() {
        let before = json!({"requests": 1, "memories": 2, "review": 0});
        let now = json!({"requests": 1, "memories": 1, "review": 3});
        assert_eq!(rises(&before, &now), vec!["3 items wait in Review"]);
    }

    #[test]
    fn one_reads_as_one() {
        let before = json!({"requests": 0, "memories": 0, "review": 0});
        let now = json!({"requests": 1, "memories": 0, "review": 0});
        assert_eq!(rises(&before, &now), vec!["1 agent request waits for you"]);
    }

    #[test]
    fn the_total_ignores_what_it_does_not_know() {
        assert_eq!(
            total(&json!({"requests": 2, "memories": 1, "review": 4, "later": 9})),
            7
        );
    }
}
