import Foundation
import Network

final class DeviceDiscovery {
    private var browser: NWBrowser?
    private let queue = DispatchQueue(label: "network.autonomous.ai.buddy.discovery")

    var onDevicesChanged: (([DeviceInfo]) -> Void)?

    func start() {
        stop()
        let parameters = NWParameters()
        parameters.includePeerToPeer = false
        let browser = NWBrowser(for: .bonjour(type: "_autonomous._tcp", domain: nil), using: parameters)
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            self?.handle(results: results)
        }
        browser.stateUpdateHandler = { state in
            if case .failed(let error) = state {
                NSLog("DeviceDiscovery failed: \(error.localizedDescription)")
            }
        }
        browser.start(queue: queue)
        self.browser = browser
    }

    func stop() {
        browser?.cancel()
        browser = nil
    }

    private func handle(results: Set<NWBrowser.Result>) {
        var devices: [DeviceInfo] = []
        let now = Date()
        for result in results {
            guard case .service(let name, _, _, _) = result.endpoint else { continue }
            // `<name>.local` resolves via mDNS; port comes from TXT later, default 80.
            let host = name.hasSuffix(".local") ? name : "\(name).local"
            devices.append(DeviceInfo(name: name, host: host, port: 80, discoveredAt: now))
        }
        let unique = Array(Set(devices)).sorted { $0.host < $1.host }
        DispatchQueue.main.async { [weak self] in
            self?.onDevicesChanged?(unique)
        }
    }
}
