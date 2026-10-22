//! The few AppKit calls Tauri does not make for us.

use std::ffi::{c_char, c_void};

use tauri::WebviewWindow;

/// `NSWindowCollectionBehaviorFullScreenPrimary`.
const FULL_SCREEN_PRIMARY: u64 = 1 << 7;

#[link(name = "objc")]
extern "C" {
    fn sel_registerName(name: *const c_char) -> *const c_void;
    fn objc_msgSend();
}

/// Let the window take a full-screen space of its own (green button,
/// View › Enter Full Screen, ⌃⌘F), whatever AppKit infers by default.
pub fn allow_full_screen(window: &WebviewWindow) {
    let Ok(ns_window) = window.ns_window() else {
        return;
    };
    let address = ns_window as usize;
    let _ = window.run_on_main_thread(move || unsafe {
        let get: extern "C" fn(*mut c_void, *const c_void) -> u64 =
            std::mem::transmute(objc_msgSend as *const ());
        let set: extern "C" fn(*mut c_void, *const c_void, u64) =
            std::mem::transmute(objc_msgSend as *const ());
        let window = address as *mut c_void;
        let behavior = get(window, sel_registerName(c"collectionBehavior".as_ptr()));
        set(
            window,
            sel_registerName(c"setCollectionBehavior:".as_ptr()),
            behavior | FULL_SCREEN_PRIMARY,
        );
    });
}
