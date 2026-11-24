import AppKit
import UserNotifications

final class DesktopNotifications: NSObject, UNUserNotificationCenterDelegate {
    private var openMainWindow: (@MainActor () -> Void)?

    @MainActor
    func configure(openMainWindow: @escaping @MainActor () -> Void) {
        self.openMainWindow = openMainWindow
        UNUserNotificationCenter.current().delegate = self
    }

    func post(id: String, title: String, body: String) {
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert]) { allowed, error in
            if let error {
                NSLog("Safe Code notification authorization: %@", error.localizedDescription)
                return
            }
            guard allowed else { return }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil)) { error in
                if let error { NSLog("Safe Code notification delivery: %@", error.localizedDescription) }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completion: @escaping (UNNotificationPresentationOptions) -> Void) {
        completion([.banner, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completion: @escaping () -> Void) {
        Task { @MainActor in
            if response.actionIdentifier == UNNotificationDefaultActionIdentifier { openMainWindow?() }
            completion()
        }
    }
}
