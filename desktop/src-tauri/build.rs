fn main() {
    // App commands callable from the app's own pages and from the editor
    // served by the local engine; each gets an `allow-<command>` permission
    // that the capabilities grant.
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "app_status",
            "start_setup",
            "cancel_setup",
            "restart_engine",
            "reveal_logs",
            "document_opened",
            "recent_documents",
            "clear_recent_documents",
            "open_document",
            "choose_new_project",
            "choose_document_location",
            "choose_export_path",
            "choose_source",
            "confirm_dialog",
            "open_external",
            "reveal_path",
            "flush_done",
            "take_pending_action",
            "automation_result",
        ]),
    ))
    .expect("failed to run tauri-build");
}
