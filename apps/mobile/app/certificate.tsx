import { ScrollView, Text } from 'react-native';
import { ui } from '../src/ui';

export default function Certificate() {
	return (
		<ScrollView contentContainerStyle={ui.page}>
			<Text style={ui.title}>Trust your own laptop.</Text>
			<Text style={ui.text}>
				Chronos uses HTTPS on your home Wi-Fi. If a connection fails, check that the laptop is
				running, both devices use the same Wi-Fi, and the URL matches the certificate.
			</Text>
			<Text style={ui.subtitle}>1. Get the public root</Text>
			<Text style={ui.text}>
				On your laptop run just certs. Transfer only data/certs/rootCA.pem through a trusted channel
				such as AirDrop. Never transfer rootCA-key.pem or chronos-lan-key.pem, and never publish the
				certificate folder.
			</Text>
			<Text style={ui.subtitle}>2. Check before installing</Text>
			<Text style={ui.text}>
				Compare the certificate details and SHA-256 fingerprint with your laptop over a separate
				trusted channel. To inspect the public root on the laptop, run: openssl x509 -in
				data/certs/rootCA.pem -noout -subject -fingerprint -sha256
			</Text>
			<Text style={ui.text}>
				The certificate script prints the server leaf fingerprint, not the root fingerprint. The
				pairing QR contains no certificate or fingerprint. This app uses iOS certificate trust; it
				does not inspect or pin the peer fingerprint.
			</Text>
			<Text style={ui.subtitle}>3. Install and enable trust</Text>
			<Text style={ui.text}>
				Open the transferred root on iPhone, install its profile in Settings → General → VPN &
				Device Management, then enable full trust in Settings → General → About → Certificate Trust
				Settings. Trust only the root you verified from your own laptop.
			</Text>
			<Text style={ui.subtitle}>4. Return to pairing</Text>
			<Text style={ui.text}>
				Use the exact HTTPS /v1 URL printed by chronos pair-code. Do not switch to HTTP or bypass
				certificate errors. Simulator setup and physical iPhone setup are separate; a Simulator
				result does not prove phone trust or notification delivery.
			</Text>
		</ScrollView>
	);
}
