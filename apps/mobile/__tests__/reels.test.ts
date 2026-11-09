import { createApi, type Transport } from '../src/api/client';
import {
	type CaptionDoc,
	captionAt,
	isCaptionDoc,
	isReelDetail,
	isReelList,
	reelIdFrom,
	type SceneSummary,
	sameOrigin,
	sceneAt,
	seekTarget,
} from '../src/reels/model';

const base = 'https://laptop.local:8443/v1';
const token = 'a'.repeat(43);
const RID = '01M6ZGPR87B1GED8CV5QYJJ7RA';
const summary = {
	public_id: RID,
	local_date: '2026-10-06',
	status: 'ready',
	duration_sec: 58.2,
	saved: false,
	expires_at: '2026-10-07T06:00:00Z',
	thumbnail_url: `${base}/media/${RID}/thumb.jpg?exp=1&sig=x`,
};
const scenes: SceneSummary[] = [
	{
		index: 0,
		type: 'cover',
		title: 'Morning Reel',
		source: null,
		url: null,
		start_sec: 0,
		end_sec: 3,
	},
	{
		index: 1,
		type: 'story',
		title: 'A',
		source: 'BBC News',
		url: 'https://bbc.co.uk/a',
		start_sec: 3,
		end_sec: 12,
	},
	{ index: 2, type: 'outro', title: 'Bye', source: null, url: null, start_sec: 12, end_sec: 15 },
];
const detail = {
	...summary,
	media_url: `${base}/media/${RID}/reel.mp4?exp=1&sig=x`,
	captions_url: null,
	scenes,
};
const response = (data: unknown, status = 200) =>
	new Response(status === 204 ? null : JSON.stringify(data), {
		status,
		headers: { 'content-type': 'application/json' },
	});

describe('validation', () => {
	test('accepts well-formed reel responses', () => {
		expect(isReelList([summary])).toBe(true);
		expect(isReelDetail(detail)).toBe(true);
	});
	test('rejects bad ids, shapes and scene order', () => {
		expect(isReelList([{ ...summary, public_id: '../etc' }])).toBe(false);
		expect(isReelList({})).toBe(false);
		expect(isReelDetail({ ...detail, scenes: [{ ...scenes[0], end_sec: -1 }] })).toBe(false);
		expect(isReelDetail(summary)).toBe(false);
	});
	test('caption documents', () => {
		expect(isCaptionDoc({ scenes: [{ index: 0, words: [{ w: 'Hi', s: 0, e: 1 }] }] })).toBe(true);
		expect(isCaptionDoc({ scenes: [{ index: 0, words: [{ w: 'Hi' }] }] })).toBe(false);
	});
});

describe('pager state', () => {
	test('scene at a time and seek targets', () => {
		expect(sceneAt(scenes, 0)).toBe(0);
		expect(sceneAt(scenes, 3)).toBe(1);
		expect(sceneAt(scenes, 14)).toBe(2);
		expect(seekTarget(scenes, 1)).toBeCloseTo(3.05);
		expect(seekTarget(scenes, 99)).toBeCloseTo(12.05);
		expect(seekTarget([], 0)).toBe(0);
	});
});

describe('caption overlay', () => {
	const words = ['BBC', 'News', 'reports', 'heavy', 'rain', 'across', 'central', 'Europe.'].map(
		(w, i) => ({ w, s: 3 + i * 0.5, e: 3.5 + i * 0.5 }),
	);
	const doc: CaptionDoc = { scenes: [{ index: 1, words }] };
	test('shows the line containing the spoken word with it highlighted', () => {
		const at = captionAt(doc, 4.1);
		expect(at?.words.map((w) => w.w)).toEqual(['BBC', 'News', 'reports', 'heavy', 'rain']);
		expect(at?.active).toBe(2);
		expect(captionAt(doc, 5.6)?.words[0]?.w).toBe('across');
	});
	test('nothing before the first word, briefly holds after the last, then clears', () => {
		expect(captionAt(doc, 1)).toBeNull();
		expect(captionAt(doc, 7.2)?.active).toBe(-1);
		expect(captionAt(doc, 10)).toBeNull();
		expect(captionAt(null, 4)).toBeNull();
	});
});

describe('links', () => {
	test('media must come from the paired laptop', () => {
		expect(sameOrigin(detail.media_url, base)).toBe(true);
		expect(sameOrigin('https://evil.example/x.mp4', base)).toBe(false);
		expect(sameOrigin('not a url', base)).toBe(false);
	});
	test('deep links and notification payloads', () => {
		expect(reelIdFrom(`chronos://reel/${RID}`)).toBe(RID);
		expect(reelIdFrom('chronos://reel/../../x')).toBeNull();
		expect(reelIdFrom({ type: 'reel_ready', reel_public_id: RID })).toBe(RID);
		expect(reelIdFrom({ type: 'tick' })).toBeNull();
	});
});

describe('api client reel endpoints', () => {
	test('list, detail, save, unsave and delete hit only approved bearer paths', async () => {
		const transport = jest.fn<ReturnType<Transport>, Parameters<Transport>>();
		transport
			.mockResolvedValueOnce(response([summary]))
			.mockResolvedValueOnce(response(detail))
			.mockResolvedValueOnce(response({ ...summary, saved: true, expires_at: null }))
			.mockResolvedValueOnce(response(summary))
			.mockResolvedValueOnce(response(null, 204));
		const api = createApi(base, transport, token);
		expect(await api.listReels()).toHaveLength(1);
		expect((await api.getReel(RID)).scenes).toHaveLength(3);
		expect((await api.setSaved(RID, true)).saved).toBe(true);
		expect((await api.setSaved(RID, false)).saved).toBe(false);
		await api.deleteReel(RID);
		expect(transport.mock.calls.map(([r]) => [r.method, r.url.replace(base, '')])).toEqual([
			['GET', '/reels?limit=20'],
			['GET', `/reels/${RID}`],
			['POST', `/reels/${RID}/save`],
			['DELETE', `/reels/${RID}/save`],
			['DELETE', `/reels/${RID}`],
		]);
		expect(
			transport.mock.calls.every(([r]) => r.headers.get('authorization') === `Bearer ${token}`),
		).toBe(true);
	});
	test('a malformed id never leaves the phone', async () => {
		const transport = jest.fn<ReturnType<Transport>, Parameters<Transport>>();
		await expect(createApi(base, transport, token).getReel('not-a-ulid')).rejects.toThrow(
			'not approved',
		);
		expect(transport).not.toHaveBeenCalled();
	});
	test('an invalid reel response is rejected', async () => {
		const api = createApi(base, async () => response({ nope: true }), token);
		await expect(api.getReel(RID)).rejects.toThrow('invalid reel response');
	});
});
