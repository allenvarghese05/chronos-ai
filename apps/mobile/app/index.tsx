import { StyleSheet, Text, View } from 'react-native';

import ChronosSensors from '../modules/chronos-sensors/src';

export default function Index() {
	const greeting = ChronosSensors.hello();

	return (
		<View style={styles.container}>
			<Text style={styles.text}>{greeting}</Text>
		</View>
	);
}

const styles = StyleSheet.create({
	container: {
		alignItems: 'center',
		flex: 1,
		justifyContent: 'center',
	},
	text: {
		fontSize: 18,
	},
});
