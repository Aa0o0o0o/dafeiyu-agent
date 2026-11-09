import asyncio,base64,io,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from docx import Document
from src.vision import parse_message,ImageInputError
from src.group_media import extract_document,extract_video,run_process
from src.reply_context import parse_quoted
from tests import test_group_chat as groups, test_open_contacts as contacts
from tests.test_conversation import CONFIG

ROOT=Path(__file__).resolve().parents[1]

class MediaParsingTests(unittest.TestCase):
    def test_group_media_is_only_accepted_in_group_path(self):
        raw=[{'type':'video','data':{'file':'only-qq-token','file_size':'100'}}]
        with self.assertRaises(ImageInputError): parse_message(raw)
        parsed=parse_message(raw,allow_group_media=True)
        self.assertEqual(parsed.attachments,('video',))
        self.assertNotIn('only-qq-token',parsed.history_text)
        quoted={'message_id':1,'message_type':'group','group_id':groups.GROUP,
                'sender':{'user_id':'12345678'},'message':raw}
        self.assertEqual(parse_quoted(quoted,1,bot_qq=CONFIG['bot_qq'],
                         owner_qq=CONFIG['owner_qq'],group_id=groups.GROUP)['attachments'],('video',))
        with self.assertRaises(ValueError):
            parse_quoted(quoted,1,bot_qq=CONFIG['bot_qq'],owner_qq=CONFIG['owner_qq'],group_id=groups.OTHER)

class LocalMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_document_are_read_as_data(self):
        result=await extract_document('合成的群文件内容'.encode(),'notes.txt',ROOT)
        self.assertIn('合成的群文件内容',result['text'])
        doc=Document();doc.add_paragraph('合成文档中的说明'); data=io.BytesIO();doc.save(data)
        result=await extract_document(data.getvalue(),'report.docx',ROOT)
        self.assertIn('合成文档中的说明',result['text'])
        with self.assertRaises(ImageInputError): await extract_document(b'fake','run.exe',ROOT)

    async def test_synthetic_video_extracts_frames_without_claiming_full_coverage(self):
        ff=ROOT/'tools/voice/gsv-env/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp') as folder:
            path=Path(folder)/'sample.mp4'
            code,_,_=await run_process([ff,'-nostdin','-loglevel','error','-y','-f','lavfi',
                  '-i','color=c=red:s=160x90:d=2','-c:v','mpeg4',str(path)],timeout=20,cwd=ROOT)
            self.assertEqual(code,0)
            result=await extract_video(path.read_bytes(),ROOT)
            self.assertIn('非完整观看',result['text'])
            self.assertEqual(sum(x['type']=='image_url' for x in result['images']),4)

    async def test_video_audio_is_given_to_local_recognizer(self):
        ff=ROOT/'tools/voice/gsv-env/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp') as folder:
            path=Path(folder)/'sample-audio.mp4'
            code,_,_=await run_process([ff,'-nostdin','-loglevel','error','-y','-f','lavfi',
                '-i','color=c=red:s=160x90:d=2','-f','lavfi','-i','sine=frequency=440:duration=2',
                '-shortest','-c:v','mpeg4','-c:a','aac',str(path)],timeout=20,cwd=ROOT)
            self.assertEqual(code,0)
            mock=AsyncMock(return_value={'text':'合成识别结果','source':'local_whisper'})
            with patch('src.group_media.transcribe_wav',new=mock):
                result=await extract_video(path.read_bytes(),ROOT)
            mock.assert_awaited_once()
            self.assertTrue(mock.await_args.args[0].startswith(b'RIFF'))
            self.assertIn('合成识别结果',result['text'])

class GroupMediaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=groups.GroupTests.asyncSetUp
    asyncTearDown=groups.GroupTests.asyncTearDown
    receive=groups.GroupTests.receive
    async def test_at_video_loads_only_the_current_group_message(self):
        self.group_channel.read_group_media=AsyncMock(return_value=[{'text':'[合成视频画面]','images':[]}])
        event=groups.group_event('看看这个视频',mid='123',member='12345678')
        event['message'].append({'type':'video','data':{'file':'abc','file_size':'100'}})
        await self.receive(event)
        self.group_channel.read_group_media.assert_awaited_once_with('123','12345678')
        self.assertIn('合成视频画面',str(self.model.calls))
    async def test_recent_group_upload_is_read_only_when_requested(self):
        self.group.note_group_upload({'post_type':'notice','notice_type':'group_upload',
            'self_id':CONFIG['bot_qq'],'group_id':groups.GROUP,'user_id':'12345678',
            'time':time.time(),'file':{'id':'qq-upload-token','name':'合成说明.txt','size':20}})
        self.group_channel.read_group_upload=AsyncMock(return_value=[{'text':'合成文件内容','images':[]}])
        await self.receive(groups.group_event('看看刚才的文件',mid='125'))
        self.group_channel.read_group_upload.assert_awaited_once_with('qq-upload-token')
        payload=str(self.model.calls)
        self.assertIn('合成文件内容',payload)
        self.assertNotIn('qq-upload-token',payload)

    async def test_group_upload_from_other_group_is_not_retained(self):
        self.group.note_group_upload({'post_type':'notice','notice_type':'group_upload',
            'self_id':CONFIG['bot_qq'],'group_id':groups.OTHER,'user_id':'12345678',
            'time':time.time(),'file':{'id':'foreign-token','name':'a.txt','size':20}})
        self.assertIsNone(self.group.known_group_upload('foreign-token'))

    async def test_quote_video_reads_quoted_sender_and_message(self):
        self.group_channel.fetch_quoted=AsyncMock(return_value={'message_id':'-17',
            'author_qq':'87654321','author_name':'合成成员','text':'','images':(),
            'attachments':('video',)})
        self.group_channel.read_group_media=AsyncMock(return_value=[{'text':'[合成引用视频]','images':[]}])
        event=groups.group_event('帮我看看',mid='127',member='12345678')
        event['message'].append({'type':'reply','data':{'id':-17}})
        await self.receive(event)
        self.group_channel.read_group_media.assert_awaited_once_with('-17','87654321')
        self.assertIn('合成引用视频',str(self.model.calls))

    async def test_unmentioned_video_is_not_downloaded(self):
        self.group_channel.read_group_media=AsyncMock()
        event=groups.group_event('',mid='124',mentioned=False)
        event['message']=[{'type':'video','data':{'file':'abc','file_size':'100'}}]
        await self.receive(event)
        self.group_channel.read_group_media.assert_not_awaited()
        self.assertEqual(self.model.calls,[])

class GroupMediaWireTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=contacts.AccessTests.asyncSetUp
    asyncTearDown=contacts.AccessTests.asyncTearDown
    async def test_upload_must_be_registered_in_the_same_group(self):
        group=await self.adapter.target_for(groups.group_event('查看',mid='126',member=contacts.A))
        group.note_group_upload({'post_type':'notice','notice_type':'group_upload',
            'self_id':CONFIG['bot_qq'],'group_id':groups.GROUP,'user_id':contacts.A,
            'time':time.time(),'file':{'id':'qq-upload-id','name':'note.txt','size':4}})
        self.adapter.bot.call_action=AsyncMock(return_value={'base64':base64.b64encode(b'test').decode()})
        with patch('src.group_media.extract_document',new=AsyncMock(return_value={'text':'ok','images':[]})):
            result=await group.channel.read_group_upload('qq-upload-id')
        self.assertEqual(result[0]['text'],'ok')
        with self.assertRaises(ImageInputError): await group.channel.read_group_upload('foreign-id')
        self.assertEqual(self.adapter.bot.call_action.await_count,1)

    async def test_media_fetch_checks_group_and_sender_before_downloading(self):
        event=groups.group_event('看看文件',mid='123',member=contacts.A)
        group=await self.adapter.target_for(event)
        msg={'message_id':123,'message_type':'group','group_id':groups.GROUP,
             'sender':{'user_id':contacts.A},'message':[{'type':'file','data':
             {'file':'note.txt','file_id':'qq-verified-id','file_size':'4'}}]}
        async def call(action,**kwargs):
            if action=='get_msg':return msg
            if action=='get_file':return {'base64':base64.b64encode(b'test').decode()}
            raise AssertionError(action)
        self.adapter.bot.call_action=AsyncMock(side_effect=call)
        with patch('src.group_media.extract_document',new=AsyncMock(return_value={'text':'ok','images':[]})):
            result=await group.channel.read_group_media('123',contacts.A)
        self.assertEqual(result[0]['text'],'ok')
        self.adapter.bot.call_action.reset_mock();msg['group_id']=groups.OTHER
        with self.assertRaises(ImageInputError): await group.channel.read_group_media('123',contacts.A)
        self.assertEqual([x.args[0] for x in self.adapter.bot.call_action.await_args_list],['get_msg'])
