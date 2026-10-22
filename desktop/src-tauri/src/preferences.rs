//! What the app remembers for the user across launches: the appearance
//! (System, Light or Dark) and the editor's small preferences, such as the
//! playback speed.  The editor's page cannot keep them itself: its origin is
//! the engine's address, whose port changes on every launch.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};
use tauri::window::Color;
use tauri::Theme;

pub const APPEARANCES: &[&str] = &["system", "light", "dark"];
/// Keys and values the editor may store, and how many.
const MAX_KEY: usize = 64;
const MAX_VALUE: usize = 1024;
const MAX_ENTRIES: usize = 64;

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Preferences {
    #[serde(default = "system")]
    pub appearance: String,
    #[serde(default)]
    pub editor: BTreeMap<String, String>,
}

fn system() -> String {
    "system".into()
}

impl Default for Preferences {
    fn default() -> Self {
        Self {
            appearance: system(),
            editor: BTreeMap::new(),
        }
    }
}

impl Preferences {
    pub fn load(path: &Path) -> Self {
        let mut preferences: Self = fs::read_to_string(path)
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_default();
        if !APPEARANCES.contains(&preferences.appearance.as_str()) {
            preferences.appearance = system();
        }
        preferences
    }

    pub fn save(&self, path: &Path) -> Result<(), String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|error| error.to_string())?;
        }
        let text = serde_json::to_string_pretty(self).map_err(|error| error.to_string())?;
        let partial = path.with_extension("json.partial");
        fs::write(&partial, text).map_err(|error| error.to_string())?;
        fs::rename(&partial, path).map_err(|error| error.to_string())
    }

    pub fn set_appearance(&mut self, appearance: &str) -> Result<(), String> {
        if !APPEARANCES.contains(&appearance) {
            return Err(format!("unknown appearance “{appearance}”"));
        }
        self.appearance = appearance.to_string();
        Ok(())
    }

    /// Store an editor preference; `None` forgets it.
    pub fn set_editor(&mut self, key: &str, value: Option<String>) -> Result<(), String> {
        if key.is_empty() || key.len() > MAX_KEY {
            return Err("a preference needs a short name".into());
        }
        match value {
            None => {
                self.editor.remove(key);
            }
            Some(value) if value.len() > MAX_VALUE => {
                return Err(format!("the preference “{key}” is too long"));
            }
            Some(value) => {
                if !self.editor.contains_key(key) && self.editor.len() >= MAX_ENTRIES {
                    return Err("the app keeps no more preferences".into());
                }
                self.editor.insert(key.to_string(), value);
            }
        }
        Ok(())
    }

    /// The windows' theme: `None` follows the system.
    pub fn theme(&self) -> Option<Theme> {
        match self.appearance.as_str() {
            "light" => Some(Theme::Light),
            "dark" => Some(Theme::Dark),
            _ => None,
        }
    }
}

/// What a window shows before its page paints: the editor's background.
pub fn background(theme: Theme) -> Color {
    match theme {
        Theme::Light => Color(244, 245, 247, 255),
        _ => Color(17, 18, 21, 255),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn appearance_is_validated_and_survives_a_reload() {
        let directory = std::env::temp_dir().join(format!("voicecut-prefs-{}", std::process::id()));
        let path = directory.join("preferences.json");
        let mut preferences = Preferences::load(&path);
        assert_eq!(preferences, Preferences::default());
        assert!(preferences.set_appearance("sepia").is_err());
        preferences.set_appearance("light").unwrap();
        preferences
            .set_editor("voicecut.playbackRate", Some("1.5".into()))
            .unwrap();
        preferences.save(&path).unwrap();
        let reloaded = Preferences::load(&path);
        assert_eq!(reloaded.theme(), Some(Theme::Light));
        assert_eq!(
            reloaded
                .editor
                .get("voicecut.playbackRate")
                .map(String::as_str),
            Some("1.5")
        );
        fs::write(&path, r#"{"appearance": "neon"}"#).unwrap();
        assert_eq!(Preferences::load(&path).theme(), None);
        let _ = fs::remove_dir_all(directory);
    }

    #[test]
    fn editor_preferences_are_bounded() {
        let mut preferences = Preferences::default();
        assert!(preferences.set_editor("", Some("1".into())).is_err());
        assert!(preferences
            .set_editor("k", Some("x".repeat(MAX_VALUE + 1)))
            .is_err());
        for index in 0..MAX_ENTRIES {
            preferences
                .set_editor(&format!("k{index}"), Some("1".into()))
                .unwrap();
        }
        assert!(preferences
            .set_editor("one-more", Some("1".into()))
            .is_err());
        preferences.set_editor("k0", None).unwrap();
        assert!(preferences.set_editor("one-more", Some("1".into())).is_ok());
    }
}
