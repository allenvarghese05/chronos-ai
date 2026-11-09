import { useLocalSearchParams } from 'expo-router';
import { Text, View } from 'react-native';
import { PUBLIC_ID } from '../../src/reels/model';
import { ReelPlayer } from '../../src/reels/player';
import { ProtectedScene } from '../../src/session/provider';

export default function ReelScreen() {
	const { id } = useLocalSearchParams<{ id: string }>();
	return (
		<ProtectedScene>
			{typeof id === 'string' && PUBLIC_ID.test(id) ? (
				<ReelPlayer id={id} />
			) : (
				<View>
					<Text>Unknown reel.</Text>
				</View>
			)}
		</ProtectedScene>
	);
}
