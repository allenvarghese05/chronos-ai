import { StyleSheet } from 'react-native';

/** Shared quiet, readable styles for the small setup flow. */
export const ui = StyleSheet.create({
	page: { flexGrow: 1, backgroundColor: '#faf9f6', padding: 24, gap: 18 },
	title: { fontSize: 30, fontWeight: '600', color: '#202722' },
	subtitle: { fontSize: 18, fontWeight: '600', color: '#202722' },
	text: { fontSize: 16, lineHeight: 24, color: '#414943' },
	label: { fontSize: 14, fontWeight: '600', color: '#414943' },
	input: {
		backgroundColor: '#fff',
		borderColor: '#b5bbb5',
		borderWidth: 1,
		borderRadius: 8,
		padding: 14,
		fontSize: 17,
		color: '#202722',
	},
	notice: { fontSize: 15, lineHeight: 23, color: '#735231' },
	host: { fontSize: 17, lineHeight: 24, fontWeight: '600', color: '#245c43' },
	camera: { height: 300, width: '100%', borderRadius: 12, overflow: 'hidden' },
});
