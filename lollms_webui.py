"""
File: lollms_web_ui.py
Author: ParisNeo
Description: Singleton class for the LoLLMS web UI.

This class provides a singleton instance of the LoLLMS web UI, allowing access to its functionality and data across multiple endpoints.
"""

from lollms.server.elf_server import LOLLMSElfServer
from flask import request
from datetime import datetime
from api.db import DiscussionsDB, Discussion
from pathlib import Path
from lollms.config import InstallOption
from lollms.types import MSG_TYPE, SENDER_TYPES
from lollms.extension import LOLLMSExtension, ExtensionBuilder
from lollms.personality import AIPersonality, PersonalityBuilder
from lollms.binding import LOLLMSConfig, BindingBuilder, LLMBinding, ModelBuilder, BindingType
from lollms.paths import LollmsPaths
from lollms.helpers import ASCIIColors, trace_exception
from lollms.com import NotificationType, NotificationDisplayType, LoLLMsCom
from lollms.app import LollmsApplication
from lollms.utilities import File64BitsManager, PromptReshaper, PackageManager, find_first_available_file_index, run_async, is_asyncio_loop_running
import git
import asyncio
import os
try:
    from lollms.media import WebcamImageSender, AudioRecorder
    Media_on=True
except:
    ASCIIColors.warning("Couldn't load media library.\nYou will not be able to perform any of the media linked operations. please verify the logs and install any required installations")
    Media_on=False

from safe_store import TextVectorizer, VectorizationMethod, VisualizationMethod
import threading
from tqdm import tqdm 
import traceback
import sys
import gc
import ctypes
from functools import partial
import json
import shutil
import re
import string
import requests
from datetime import datetime
from typing import List, Tuple
import time
import numpy as np
from lollms.utilities import find_first_available_file_index, convert_language_name

if not PackageManager.check_package_installed("requests"):
    PackageManager.install_package("requests")
if not PackageManager.check_package_installed("bs4"):
    PackageManager.install_package("beautifulsoup4")
import requests
from flask_socketio import SocketIO
from bs4 import BeautifulSoup



def terminate_thread(thread):
    if thread:
        if not thread.is_alive():
            ASCIIColors.yellow("Thread not alive")
            return

        thread_id = thread.ident
        exc = ctypes.py_object(SystemExit)
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, exc)
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, None)
            del thread
            gc.collect()
            raise SystemError("Failed to terminate the thread.")
        else:
            ASCIIColors.yellow("Canceled successfully")# The current version of the webui
lollms_webui_version="9.0 (alpha)"



class LOLLMSWebUI(LOLLMSElfServer):
    __instance = None

    @staticmethod
    def build_instance(
        config: LOLLMSConfig,
        lollms_paths: LollmsPaths,
        load_binding=True,
        load_model=True,
        load_voice_service=True,
        load_sd_service=True,
        try_select_binding=False,
        try_select_model=False,
        callback=None,
        socketio = None
    ):
        if LOLLMSWebUI.__instance is None:
            LOLLMSWebUI(
                config,
                lollms_paths,
                load_binding=load_binding,
                load_model=load_model,
                load_sd_service=load_sd_service,
                load_voice_service=load_voice_service,
                try_select_binding=try_select_binding,
                try_select_model=try_select_model,
                callback=callback,
                socketio=socketio
            )
        return LOLLMSWebUI.__instance    
    def __init__(
        self,
        config: LOLLMSConfig,
        lollms_paths: LollmsPaths,
        load_binding=True,
        load_model=True,
        load_voice_service=True,
        load_sd_service=True,
        try_select_binding=False,
        try_select_model=False,
        callback=None,
        socketio=None
    ) -> None:
        super().__init__(
            config,
            lollms_paths,
            load_binding=load_binding,
            load_model=load_model,
            load_sd_service=load_sd_service,
            load_voice_service=load_voice_service,
            try_select_binding=try_select_binding,
            try_select_model=try_select_model,
            callback=callback,
            socketio=socketio
        )
        self.app_name:str = "LOLLMSWebUI"
        self.version:str = lollms_webui_version


        self.busy = False
        self.nb_received_tokens = 0
        
        self.config_file_path = config.file_path
        self.cancel_gen = False

        
        if self.config.auto_update:
            if self.check_update_():
                ASCIIColors.info("New version found. Updating!")
                self.run_update_script()
        # Keeping track of current discussion and message
        self._current_user_message_id = 0
        self._current_ai_message_id = 0
        self._message_id = 0

        self.db_path = config["db_path"]
        if Path(self.db_path).is_absolute():
            # Create database object
            self.db = DiscussionsDB(self.db_path)
        else:
            # Create database object
            self.db = DiscussionsDB(self.lollms_paths.personal_databases_path/self.db_path)

        # If the database is empty, populate it with tables
        ASCIIColors.info("Checking discussions database... ",end="")
        self.db.create_tables()
        self.db.add_missing_columns()
        ASCIIColors.success("ok")

        # prepare vectorization
        if self.config.data_vectorization_activate and self.config.use_discussions_history:
            try:
                ASCIIColors.yellow("Loading long term memory")
                folder = self.lollms_paths.personal_databases_path/"vectorized_dbs"
                folder.mkdir(parents=True, exist_ok=True)
                self.build_long_term_skills_memory()
                ASCIIColors.yellow("Ready")

            except Exception as ex:
                trace_exception(ex)
                self.long_term_memory = None
        else:
            self.long_term_memory = None

        # This is used to keep track of messages 
        self.download_infos={}
        
        self.connections = {
            0:{
                "current_discussion":None,
                "generated_text":"",
                "cancel_generation": False,          
                "generation_thread": None,
                "processing":False,
                "schedule_for_deletion":False,
                "continuing": False,
                "first_chunk": True,
            }
        }
        if Media_on:
            try:
                self.webcam = WebcamImageSender(socketio,lollmsCom=self)
            except:
                self.webcam = None
            try:
                self.rec_output_folder = lollms_paths.personal_outputs_path/"audio_rec"
                self.rec_output_folder.mkdir(exist_ok=True, parents=True)
                self.summoned = False
                self.audio_cap = AudioRecorder(socketio,self.rec_output_folder/"rt.wav", callback=self.audio_callback,lollmsCom=self)
            except:
                self.audio_cap = None
                self.rec_output_folder = None
        else:
            self.webcam = None
            self.rec_output_folder = None



        # Define a WebSocket event handler
        @socketio.event
        async def connect(sid, environ):
            #Create a new connection information
            self.connections[sid] = {
                "current_discussion":self.db.load_last_discussion(),
                "generated_text":"",
                "continuing": False,
                "first_chunk": True,
                "cancel_generation": False,          
                "generation_thread": None,
                "processing":False,
                "schedule_for_deletion":False
            }
            await self.socketio.emit('connected', to=sid) 
            ASCIIColors.success(f'Client {sid} connected')

        @socketio.event
        def disconnect(sid):
            try:
                if self.connections[sid]["processing"]:
                    self.connections[sid]["schedule_for_deletion"]=True
                else:
                    del self.connections[sid]
            except Exception as ex:
                pass
            
            ASCIIColors.error(f'Client {sid} disconnected')

        # generation status
        self.generating=False
        ASCIIColors.blue(f"Your personal data is stored here :",end="")
        ASCIIColors.green(f"{self.lollms_paths.personal_path}")


    # Other methods and properties of the LoLLMSWebUI singleton class
    def check_module_update_(self, repo_path, branch_name="main"):
        try:
            # Open the repository
            ASCIIColors.yellow(f"Checking for updates from {repo_path}")
            repo = git.Repo(repo_path)
            
            # Fetch updates from the remote for the specified branch
            repo.remotes.origin.fetch(refspec=f"refs/heads/{branch_name}:refs/remotes/origin/{branch_name}")
            
            # Compare the local and remote commit IDs for the specified branch
            local_commit = repo.head.commit
            remote_commit = repo.remotes.origin.refs[branch_name].commit
            
            # Check if the local branch is behind the remote branch
            is_behind = repo.is_ancestor(local_commit, remote_commit) and local_commit!= remote_commit
            
            ASCIIColors.yellow(f"update availability: {is_behind}")
            
            # Return True if the local branch is behind the remote branch
            return is_behind
        except Exception as e:
            # Handle any errors that may occur during the fetch process
            # trace_exception(e)
            return False        
            
    def check_update_(self, branch_name="main"):
        try:
            # Open the repository
            repo_path = str(Path(__file__).parent)
            if self.check_module_update_(repo_path, branch_name):
                return True
            repo_path = str(Path(__file__).parent/"lollms_core")
            if self.check_module_update_(repo_path, branch_name):
                return True
            repo_path = str(Path(__file__).parent/"utilities/safe_store")
            if self.check_module_update_(repo_path, branch_name):
                return True
            return False
        except Exception as e:
            # Handle any errors that may occur during the fetch process
            # trace_exception(e)
            return False
                    
    def run_update_script(self, args=None):
        update_script = Path(__file__).parent/"update_script.py"

        # Convert Namespace object to a dictionary
        if args:
            args_dict = vars(args)
        else:
            args_dict = {}
        # Filter out any key-value pairs where the value is None
        valid_args = {key: value for key, value in args_dict.items() if value is not None}

        # Save the arguments to a temporary file
        temp_file = Path(__file__).parent/"temp_args.txt"
        with open(temp_file, "w") as file:
            # Convert the valid_args dictionary to a string in the format "key1 value1 key2 value2 ..."
            arg_string = " ".join([f"--{key} {value}" for key, value in valid_args.items()])
            file.write(arg_string)

        os.system(f"python {update_script}")
        sys.exit(0)


    def run_restart_script(self, args):
        restart_script = Path(__file__).parent/"restart_script.py"

        # Convert Namespace object to a dictionary
        args_dict = vars(args)

        # Filter out any key-value pairs where the value is None
        valid_args = {key: value for key, value in args_dict.items() if value is not None}

        # Save the arguments to a temporary file
        temp_file = Path(__file__).parent/"temp_args.txt"
        with open(temp_file, "w") as file:
            # Convert the valid_args dictionary to a string in the format "key1 value1 key2 value2 ..."
            arg_string = " ".join([f"--{key} {value}" for key, value in valid_args.items()])
            file.write(arg_string)

        os.system(f"python {restart_script}")
        sys.exit(0)

    def audio_callback(self, text):
        if self.summoned:
            client_id = 0
            self.cancel_gen = False
            self.connections[client_id]["generated_text"]=""
            self.connections[client_id]["cancel_generation"]=False
            self.connections[client_id]["continuing"]=False
            self.connections[client_id]["first_chunk"]=True
            
            if not self.model:
                ASCIIColors.error("Model not selected. Please select a model")
                self.error("Model not selected. Please select a model", client_id=client_id)
                return
 
            if not self.busy:
                if self.connections[client_id]["current_discussion"] is None:
                    if self.db.does_last_discussion_have_messages():
                        self.connections[client_id]["current_discussion"] = self.db.create_discussion()
                    else:
                        self.connections[client_id]["current_discussion"] = self.db.load_last_discussion()

                prompt = text
                ump = self.config.discussion_prompt_separator +self.config.user_name.strip() if self.config.use_user_name_in_discussions else self.personality.user_message_prefix
                message = self.connections[client_id]["current_discussion"].add_message(
                    message_type    = MSG_TYPE.MSG_TYPE_FULL.value,
                    sender_type     = SENDER_TYPES.SENDER_TYPES_USER.value,
                    sender          = ump.replace(self.config.discussion_prompt_separator,"").replace(":",""),
                    content=prompt,
                    metadata=None,
                    parent_message_id=self.message_id
                )

                ASCIIColors.green("Starting message generation by "+self.personality.name)
                self.connections[client_id]['generation_thread'] = threading.Thread(target=self.start_message_generation, args=(message, message.id, client_id))
                self.connections[client_id]['generation_thread'].start()
                
                self.socketio.sleep(0.01)
                ASCIIColors.info("Started generation task")
                self.busy=True
                #tpe = threading.Thread(target=self.start_message_generation, args=(message, message_id, client_id))
                #tpe.start()
            else:
                self.error("I am busy. Come back later.", client_id=client_id)
        else:
            if output["text"].lower()=="lollms":
                self.summoned = True

    def scrape_and_save(self, url, file_path):
        # Send a GET request to the URL
        response = requests.get(url)
        
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find all the text content in the webpage
        text_content = soup.get_text()
        
        # Remove extra returns and spaces
        text_content = ' '.join(text_content.split())
        
        # Save the text content as a text file
        with open(file_path, 'w', encoding="utf-8") as file:
            file.write(text_content)
        
        self.info(f"Webpage content saved to {file_path}")

    def rebuild_personalities(self, reload_all=False):
        if reload_all:
            self.mounted_personalities=[]

        loaded = self.mounted_personalities
        loaded_names = [f"{p.category}/{p.personality_folder_name}:{p.selected_language}" if p.selected_language else f"{p.category}/{p.personality_folder_name}" for p in loaded]
        mounted_personalities=[]
        ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
        ASCIIColors.success(f" ║           Building mounted Personalities         ║ ")
        ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
        to_remove=[]
        for i,personality in enumerate(self.config['personalities']):
            if i==self.config["active_personality_id"]:
                ASCIIColors.red("*", end="")
                ASCIIColors.green(f" {personality}")
            else:
                ASCIIColors.yellow(f" {personality}")
            if personality in loaded_names:
                mounted_personalities.append(loaded[loaded_names.index(personality)])
            else:
                personality_path = f"{personality}" if not ":" in personality else f"{personality.split(':')[0]}"
                try:
                    personality = AIPersonality(personality_path,
                                                self.lollms_paths, 
                                                self.config,
                                                model=self.model,
                                                app=self,
                                                selected_language=personality.split(":")[1] if ":" in personality else None,
                                                run_scripts=True)
                    mounted_personalities.append(personality)
                    if self.config.enable_voice_service and self.config.auto_read and len(personality.audio_samples)>0:
                        try:
                            from lollms.services.xtts.lollms_xtts import LollmsXTTS
                            if self.tts is None:
                                self.tts = LollmsXTTS(self, voice_samples_path=Path(__file__).parent.parent/"voices")
                        except:
                            self.warning(f"Personality {personality.name} request using custom voice but couldn't load XTTS")
                except Exception as ex:
                    ASCIIColors.error(f"Personality file not found or is corrupted ({personality_path}).\nReturned the following exception:{ex}\nPlease verify that the personality you have selected exists or select another personality. Some updates may lead to change in personality name or category, so check the personality selection in settings to be sure.")
                    ASCIIColors.info("Trying to force reinstall")
                    if self.config["debug"]:
                        print(ex)
                    try:
                        personality = AIPersonality(
                                                    personality_path, 
                                                    self.lollms_paths, 
                                                    self.config, 
                                                    self.model, 
                                                    app = self,
                                                    run_scripts=True,                                                    
                                                    selected_language=personality.split(":")[1] if ":" in personality else None,
                                                    installation_option=InstallOption.FORCE_INSTALL)
                        mounted_personalities.append(personality)
                        if personality.processor:
                            personality.processor.mounted()
                    except Exception as ex:
                        ASCIIColors.error(f"Couldn't load personality at {personality_path}")
                        trace_exception(ex)
                        ASCIIColors.info(f"Unmounting personality")
                        to_remove.append(i)
                        personality = AIPersonality(None,                                                    
                                                    self.lollms_paths, 
                                                    self.config, 
                                                    self.model,
                                                    app=self,
                                                    run_scripts=True,
                                                    installation_option=InstallOption.FORCE_INSTALL)
                        mounted_personalities.append(personality)
                        if personality.processor:
                            personality.processor.mounted()

                        ASCIIColors.info("Reverted to default personality")
        if self.config["active_personality_id"]>=0 and self.config["active_personality_id"]<len(self.config["personalities"]):
            ASCIIColors.success(f'selected model : {self.config["personalities"][self.config["active_personality_id"]]}')
        else:
            ASCIIColors.warning('An error was encountered while trying to mount personality')
        ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
        ASCIIColors.success(f" ║                      Done                        ║ ")
        ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
        # Sort the indices in descending order to ensure correct removal
        to_remove.sort(reverse=True)

        # Remove elements from the list based on the indices
        for index in to_remove:
            if 0 <= index < len(mounted_personalities):
                mounted_personalities.pop(index)
                self.config["personalities"].pop(index)
                ASCIIColors.info(f"removed personality {personality_path}")

        if self.config["active_personality_id"]>=len(self.config["personalities"]):
            self.config["active_personality_id"]=0
            
        return mounted_personalities
    

    def rebuild_extensions(self, reload_all=False):
        if reload_all:
            self.mounted_extensions=[]

        loaded = self.mounted_extensions
        loaded_names = [f"{p.category}/{p.extension_folder_name}" for p in loaded]
        mounted_extensions=[]
        ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
        ASCIIColors.success(f" ║           Building mounted Extensions            ║ ")
        ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
        to_remove=[]
        for i,extension in enumerate(self.config['extensions']):
            ASCIIColors.yellow(f" {extension}")
            if extension in loaded_names:
                mounted_extensions.append(loaded[loaded_names.index(extension)])
            else:
                extension_path = self.lollms_paths.extensions_zoo_path/f"{extension}" 
                try:
                    extension = ExtensionBuilder().build_extension(extension_path,self.lollms_paths, self)
                    mounted_extensions.append(extension)
                except Exception as ex:
                    ASCIIColors.error(f"Extension file not found or is corrupted ({extension_path}).\nReturned the following exception:{ex}\nPlease verify that the personality you have selected exists or select another personality. Some updates may lead to change in personality name or category, so check the personality selection in settings to be sure.")
                    trace_exception(ex)
                    ASCIIColors.info("Trying to force reinstall")
                    if self.config["debug"]:
                        print(ex)
        ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
        ASCIIColors.success(f" ║                      Done                        ║ ")
        ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
        # Sort the indices in descending order to ensure correct removal
        to_remove.sort(reverse=True)

        # Remove elements from the list based on the indices
        for index in to_remove:
            if 0 <= index < len(mounted_extensions):
                mounted_extensions.pop(index)
                self.config["extensions"].pop(index)
                ASCIIColors.info(f"removed personality {extension_path}")

            
        return mounted_extensions
    # ================================== LOLLMSApp

    #properties
    @property
    def message_id(self):
        return self._message_id
    @message_id.setter
    def message_id(self, id):
        self._message_id=id

    @property
    def current_user_message_id(self):
        return self._current_user_message_id
    @current_user_message_id.setter
    def current_user_message_id(self, id):
        self._current_user_message_id=id
        self._message_id = id
    @property
    def current_ai_message_id(self):
        return self._current_ai_message_id
    @current_ai_message_id.setter
    def current_ai_message_id(self, id):
        self._current_ai_message_id=id
        self._message_id = id

    def download_file(self, url, installation_path, callback=None):
        """
        Downloads a file from a URL, reports the download progress using a callback function, and displays a progress bar.

        Args:
            url (str): The URL of the file to download.
            installation_path (str): The path where the file should be saved.
            callback (function, optional): A callback function to be called during the download
                with the progress percentage as an argument. Defaults to None.
        """
        try:
            response = requests.get(url, stream=True)

            # Get the file size from the response headers
            total_size = int(response.headers.get('content-length', 0))

            with open(installation_path, 'wb') as file:
                downloaded_size = 0
                with tqdm(total=total_size, unit='B', unit_scale=True, ncols=80) as progress_bar:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            file.write(chunk)
                            downloaded_size += len(chunk)
                            if callback is not None:
                                callback(downloaded_size, total_size)
                            progress_bar.update(len(chunk))

            if callback is not None:
                callback(total_size, total_size)

            print("File downloaded successfully")
        except Exception as e:
            print("Couldn't download file:", str(e))



    def clean_string(self, input_string):
        # Remove extra spaces by replacing multiple spaces with a single space
        #cleaned_string = re.sub(r'\s+', ' ', input_string)

        # Remove extra line breaks by replacing multiple consecutive line breaks with a single line break
        cleaned_string = re.sub(r'\n\s*\n', '\n', input_string)
        # Create a string containing all punctuation characters
        punctuation_chars = string.punctuation        
        # Define a regular expression pattern to match and remove non-alphanumeric characters
        #pattern = f'[^a-zA-Z0-9\s{re.escape(punctuation_chars)}]'  # This pattern matches any character that is not a letter, digit, space, or punctuation
        pattern = f'[^a-zA-Z0-9\u00C0-\u017F\s{re.escape(punctuation_chars)}]'
        # Use re.sub to replace the matched characters with an empty string
        cleaned_string = re.sub(pattern, '', cleaned_string)
        return cleaned_string
    
    def make_discussion_title(self, discussion, client_id=None):
        """
        Builds a title for a discussion
        """
        # Get the list of messages
        messages = discussion.get_messages()
        discussion_messages = "!@>instruction: Create a short title to this discussion\n"
        discussion_title = "\n!@>Discussion title:"

        available_space = self.config.ctx_size - 150 - len(self.model.tokenize(discussion_messages))- len(self.model.tokenize(discussion_title))
        # Initialize a list to store the full messages
        full_message_list = []        
        # Accumulate messages until the cumulative number of tokens exceeds available_space
        tokens_accumulated = 0
        # Accumulate messages starting from message_index
        for message in messages:
            # Check if the message content is not empty and visible to the AI
            if message.content != '' and (
                    message.message_type <= MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_USER.value and message.message_type != MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_AI.value):

                # Tokenize the message content
                message_tokenized = self.model.tokenize(
                    "\n" + self.config.discussion_prompt_separator + message.sender + ": " + message.content.strip())

                # Check if adding the message will exceed the available space
                if tokens_accumulated + len(message_tokenized) > available_space:
                    break

                # Add the tokenized message to the full_message_list
                full_message_list.insert(0, message_tokenized)

                # Update the cumulative number of tokens
                tokens_accumulated += len(message_tokenized)

        # Build the final discussion messages by detokenizing the full_message_list
        
        for message_tokens in full_message_list:
            discussion_messages += self.model.detokenize(message_tokens)
        discussion_messages += discussion_title
        title = [""]
        def receive(
                        chunk:str, 
                        message_type:MSG_TYPE
                    ):
            if chunk:
                title[0] += chunk
            antiprompt = self.personality.detect_antiprompt(title[0])
            if antiprompt:
                ASCIIColors.warning(f"\nDetected hallucination with antiprompt: {antiprompt}")
                title[0] = self.remove_text_from_string(title[0],antiprompt)
                return False
            else:
                return True
            
        self._generate(discussion_messages, 150, client_id, receive)
        ASCIIColors.info(title[0])
        return title[0]
   

    def prepare_reception(self, client_id):
        if not self.connections[client_id]["continuing"]:
            self.connections[client_id]["generated_text"] = ""
            
        self.connections[client_id]["first_chunk"]=True
            
        self.nb_received_tokens = 0
        self.start_time = datetime.now()

    def recover_discussion(self,client_id, message_index=-1):
        messages = self.connections[client_id]["current_discussion"].get_messages()
        discussion=""
        for msg in messages:
            if message_index!=-1 and msg>message_index:
                break
            discussion += "\n" + self.config.discussion_prompt_separator + msg.sender + ": " + msg.content.strip()
        return discussion
    def prepare_query(self, client_id: str, message_id: int = -1, is_continue: bool = False, n_tokens: int = 0, generation_type = None) -> Tuple[str, str, List[str]]:
        """
        Prepares the query for the model.

        Args:
            client_id (str): The client ID.
            message_id (int): The message ID. Default is -1.
            is_continue (bool): Whether the query is a continuation. Default is False.
            n_tokens (int): The number of tokens. Default is 0.

        Returns:
            Tuple[str, str, List[str]]: The prepared query, original message content, and tokenized query.
        """

        # Get the list of messages
        messages = self.connections[client_id]["current_discussion"].get_messages()

        # Find the index of the message with the specified message_id
        message_index = -1
        for i, message in enumerate(messages):
            if message.id == message_id:
                message_index = i
                break
        
        # Define current message
        current_message = messages[message_index]

        # Build the conditionning text block
        conditionning = self.personality.personality_conditioning

        # Check if there are document files to add to the prompt
        documentation = ""
        knowledge = ""


        # boosting information
        if self.config.positive_boost:
            positive_boost="\n!@>important information: "+self.config.positive_boost+"\n"
            n_positive_boost = len(self.model.tokenize(positive_boost))
        else:
            positive_boost=""
            n_positive_boost = 0

        if self.config.negative_boost:
            negative_boost="\n!@>important information: "+self.config.negative_boost+"\n"
            n_negative_boost = len(self.model.tokenize(negative_boost))
        else:
            negative_boost=""
            n_negative_boost = 0

        if self.config.force_output_language_to_be:
            force_language="\n!@>important information: Answer the user in this language :"+self.config.force_output_language_to_be+"\n"
            n_force_language = len(self.model.tokenize(force_language))
        else:
            force_language=""
            n_force_language = 0

        if self.config.fun_mode:
            fun_mode="\n!@>important information: Fun mode activated. Don't forget to sprincle some fun in the output.\n"
            n_fun_mode = len(self.model.tokenize(positive_boost))
        else:
            fun_mode=""
            n_fun_mode = 0


        if generation_type != "simple_question":
            if self.personality.persona_data_vectorizer:
                if documentation=="":
                    documentation="!@>Documentation:\n"

                if self.config.data_vectorization_build_keys_words:
                    discussion = self.recover_discussion(client_id)[-512:]
                    query = self.personality.fast_gen(f"\n!@>instruction: Read the discussion and rewrite the last prompt for someone who didn't read the entire discussion.\nDo not answer the prompt. Do not add explanations.\n!@>discussion:\n{discussion}\n!@>enhanced query: ", max_generation_size=256, show_progress=True)
                    ASCIIColors.cyan(f"Query:{query}")
                else:
                    query = current_message.content
                try:
                    docs, sorted_similarities = self.personality.persona_data_vectorizer.recover_text(query, top_k=self.config.data_vectorization_nb_chunks)
                    for doc, infos in zip(docs, sorted_similarities):
                        documentation += f"document chunk:\n{doc}"
                except:
                    self.warning("Couldn't add documentation to the context. Please verify the vector database")
            
            if len(self.personality.text_files) > 0 and self.personality.vectorizer:
                if documentation=="":
                    documentation="!@>Documentation:\n"

                if self.config.data_vectorization_build_keys_words:
                    discussion = self.recover_discussion(client_id)[-512:]
                    query = self.personality.fast_gen(f"\n!@>instruction: Read the discussion and rewrite the last prompt for someone who didn't read the entire discussion.\nDo not answer the prompt. Do not add explanations.\n!@>discussion:\n{discussion}\n!@>enhanced query: ", max_generation_size=256, show_progress=True)
                    ASCIIColors.cyan(f"Query:{query}")
                else:
                    query = current_message.content

                try:
                    docs, sorted_similarities = self.personality.vectorizer.recover_text(query, top_k=self.config.data_vectorization_nb_chunks)
                    for doc, infos in zip(docs, sorted_similarities):
                        documentation += f"document chunk:\nchunk path: {infos[0]}\nchunk content:{doc}"
                    documentation += "\n!@>important information: Use the documentation data to answer the user questions. If the data is not present in the documentation, please tell the user that the information he is asking for does not exist in the documentation section. It is strictly forbidden to give the user an answer without having actual proof from the documentation."
                except:
                    self.warning("Couldn't add documentation to the context. Please verify the vector database")
            # Check if there is discussion knowledge to add to the prompt
            if self.config.use_discussions_history and self.long_term_memory is not None:
                if knowledge=="":
                    knowledge="!@>knowledge:\n"

                try:
                    docs, sorted_similarities = self.long_term_memory.recover_text(current_message.content, top_k=self.config.data_vectorization_nb_chunks)
                    for i,(doc, infos) in enumerate(zip(docs, sorted_similarities)):
                        knowledge += f"!@>knowledge {i}:\n!@>title:\n{infos[0]}\ncontent:\n{doc}"
                except:
                    self.warning("Couldn't add long term memory information to the context. Please verify the vector database")        # Add information about the user
        user_description=""
        if self.config.use_user_name_in_discussions:
            user_description="!@>User description:\n"+self.config.user_description+"\n"


        # Tokenize the conditionning text and calculate its number of tokens
        tokens_conditionning = self.model.tokenize(conditionning)
        n_cond_tk = len(tokens_conditionning)

        # Tokenize the documentation text and calculate its number of tokens
        if len(documentation)>0:
            tokens_documentation = self.model.tokenize(documentation)
            n_doc_tk = len(tokens_documentation)
        else:
            tokens_documentation = []
            n_doc_tk = 0

        # Tokenize the knowledge text and calculate its number of tokens
        if len(knowledge)>0:
            tokens_history = self.model.tokenize(knowledge)
            n_history_tk = len(tokens_history)
        else:
            tokens_history = []
            n_history_tk = 0


        # Tokenize user description
        if len(user_description)>0:
            tokens_user_description = self.model.tokenize(user_description)
            n_user_description_tk = len(tokens_user_description)
        else:
            tokens_user_description = []
            n_user_description_tk = 0


        # Calculate the total number of tokens between conditionning, documentation, and knowledge
        total_tokens = n_cond_tk + n_doc_tk + n_history_tk + n_user_description_tk + n_positive_boost + n_negative_boost + n_force_language + n_fun_mode

        # Calculate the available space for the messages
        available_space = self.config.ctx_size - n_tokens - total_tokens

        if self.config.debug:
            self.info(f"Tokens summary:\nConditionning:{n_cond_tk}\ndoc:{n_doc_tk}\nhistory:{n_history_tk}\nuser description:{n_user_description_tk}\nAvailable space:{available_space}",10)

        # Raise an error if the available space is 0 or less
        if available_space<1:
            self.error("Not enough space in context!!")
            raise Exception("Not enough space in context!!")

        # Accumulate messages until the cumulative number of tokens exceeds available_space
        tokens_accumulated = 0


        # Initialize a list to store the full messages
        full_message_list = []
        # If this is not a continue request, we add the AI prompt
        if not is_continue:
            message_tokenized = self.model.tokenize(
                "\n" +self.personality.ai_message_prefix.strip()
            )
            full_message_list.append(message_tokenized)
            # Update the cumulative number of tokens
            tokens_accumulated += len(message_tokenized)


        if generation_type != "simple_question":
            # Accumulate messages starting from message_index
            for i in range(message_index, -1, -1):
                message = messages[i]

                # Check if the message content is not empty and visible to the AI
                if message.content != '' and (
                        message.message_type <= MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_USER.value and message.message_type != MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_AI.value):

                    # Tokenize the message content
                    message_tokenized = self.model.tokenize(
                        "\n" + self.config.discussion_prompt_separator + message.sender + ": " + message.content.strip())

                    # Check if adding the message will exceed the available space
                    if tokens_accumulated + len(message_tokenized) > available_space:
                        break

                    # Add the tokenized message to the full_message_list
                    full_message_list.insert(0, message_tokenized)

                    # Update the cumulative number of tokens
                    tokens_accumulated += len(message_tokenized)
        else:
            message = messages[message_index]

            # Check if the message content is not empty and visible to the AI
            if message.content != '' and (
                    message.message_type <= MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_USER.value and message.message_type != MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_AI.value):

                # Tokenize the message content
                message_tokenized = self.model.tokenize(
                    "\n" + self.config.discussion_prompt_separator + message.sender + ": " + message.content.strip())

                # Add the tokenized message to the full_message_list
                full_message_list.insert(0, message_tokenized)

                # Update the cumulative number of tokens
                tokens_accumulated += len(message_tokenized)

        # Build the final discussion messages by detokenizing the full_message_list
        discussion_messages = ""
        for i in range(len(full_message_list)-1):
            message_tokens = full_message_list[i]
            discussion_messages += self.model.detokenize(message_tokens)
        
        ai_prefix = self.model.detokenize(full_message_list[-1])

        # Build the final prompt by concatenating the conditionning and discussion messages
        prompt_data = conditionning + documentation + knowledge + user_description + discussion_messages + positive_boost + negative_boost + force_language + fun_mode + ai_prefix

        # Tokenize the prompt data
        tokens = self.model.tokenize(prompt_data)

        # if this is a debug then show prompt construction details
        if self.config["debug"]:
            ASCIIColors.bold("CONDITIONNING")
            ASCIIColors.yellow(conditionning)
            ASCIIColors.bold("DOC")
            ASCIIColors.yellow(documentation)
            ASCIIColors.bold("HISTORY")
            ASCIIColors.yellow(knowledge)
            ASCIIColors.bold("DISCUSSION")
            ASCIIColors.hilight(discussion_messages,"!@>",ASCIIColors.color_yellow,ASCIIColors.color_bright_red,False)
            ASCIIColors.bold("Final prompt")
            ASCIIColors.hilight(prompt_data,"!@>",ASCIIColors.color_yellow,ASCIIColors.color_bright_red,False)
            ASCIIColors.info(f"prompt size:{len(tokens)} tokens") 
            ASCIIColors.info(f"available space after doc and knowledge:{available_space} tokens") 

            self.info(f"Tokens summary:\nPrompt size:{len(tokens)}\nTo generate:{available_space}",10)

        # Details
        context_details = {
            "conditionning":conditionning,
            "documentation":documentation,
            "knowledge":knowledge,
            "user_description":user_description,
            "discussion_messages":discussion_messages,
            "positive_boost":positive_boost,
            "negative_boost":negative_boost,
            "force_language":force_language,
            "fun_mode":fun_mode,
            "ai_prefix":ai_prefix

        }    

        # Return the prepared query, original message content, and tokenized query
        return prompt_data, current_message.content, tokens, context_details


    def get_discussion_to(self, client_id,  message_id=-1):
        messages = self.connections[client_id]["current_discussion"].get_messages()
        full_message_list = []
        ump = self.config.discussion_prompt_separator +self.config.user_name.strip() if self.config.use_user_name_in_discussions else self.personality.user_message_prefix

        for message in messages:
            if message["id"]<= message_id or message_id==-1: 
                if message["type"]!=MSG_TYPE.MSG_TYPE_FULL_INVISIBLE_TO_USER:
                    if message["sender"]==self.personality.name:
                        full_message_list.append(self.personality.ai_message_prefix+message["content"])
                    else:
                        full_message_list.append(ump + message["content"])

        link_text = "\n"# self.personality.link_text

        if len(full_message_list) > self.config["nb_messages_to_remember"]:
            discussion_messages = self.personality.personality_conditioning+ link_text.join(full_message_list[-self.config["nb_messages_to_remember"]:])
        else:
            discussion_messages = self.personality.personality_conditioning+ link_text.join(full_message_list)
        
        return discussion_messages # Removes the last return

    def notify(
                self, 
                content, 
                notification_type:NotificationType=NotificationType.NOTIF_SUCCESS, 
                duration:int=4, 
                client_id=None, 
                display_type:NotificationDisplayType=NotificationDisplayType.TOAST,
                verbose=True,
            ):
        
        run_async(partial(self.socketio.emit,'notification', {
                                'content': content,# self.connections[client_id]["generated_text"], 
                                'notification_type': notification_type.value,
                                "duration": duration,
                                'display_type':display_type.value
                            }, to=client_id
                            )
        )
        if verbose:
            if notification_type==NotificationType.NOTIF_SUCCESS:
                ASCIIColors.success(content)
            elif notification_type==NotificationType.NOTIF_INFO:
                ASCIIColors.info(content)
            elif notification_type==NotificationType.NOTIF_WARNING:
                ASCIIColors.warning(content)
            else:
                ASCIIColors.red(content)


    def new_message(self, 
                            client_id, 
                            sender=None, 
                            content="",
                            parameters=None,
                            metadata=None,
                            ui=None,
                            message_type:MSG_TYPE=MSG_TYPE.MSG_TYPE_FULL, 
                            sender_type:SENDER_TYPES=SENDER_TYPES.SENDER_TYPES_AI,
                            open=False
                        ):
        
        mtdt = metadata if metadata is None or type(metadata) == str else json.dumps(metadata, indent=4)
        if sender==None:
            sender= self.personality.name
        msg = self.connections[client_id]["current_discussion"].add_message(
            message_type        = message_type.value,
            sender_type         = sender_type.value,
            sender              = sender,
            content             = content,
            metadata            = mtdt,
            ui                  = ui,
            rank                = 0,
            parent_message_id   = self.connections[client_id]["current_discussion"].current_message.id,
            binding             = self.config["binding_name"],
            model               = self.config["model_name"], 
            personality         = self.config["personalities"][self.config["active_personality_id"]],
        )  # first the content is empty, but we'll fill it at the end  
        run_async(partial(
                    self.socketio.emit,'new_message',
                        {
                            "sender":                   sender,
                            "message_type":             message_type.value,
                            "sender_type":              SENDER_TYPES.SENDER_TYPES_AI.value,
                            "content":                  content,
                            "parameters":               parameters,
                            "metadata":                 metadata,
                            "ui":                       ui,
                            "id":                       msg.id,
                            "parent_message_id":        msg.parent_message_id,

                            'binding':                  self.config["binding_name"],
                            'model' :                   self.config["model_name"], 
                            'personality':              self.config["personalities"][self.config["active_personality_id"]],

                            'created_at':               self.connections[client_id]["current_discussion"].current_message.created_at,
                            'finished_generating_at':   self.connections[client_id]["current_discussion"].current_message.finished_generating_at,

                            'open':                     open
                        }, to=client_id
                )
            )
        
    def update_message(self, client_id, chunk,                             
                            parameters=None,
                            metadata=[], 
                            ui=None,
                            msg_type:MSG_TYPE=None
                        ):
        self.connections[client_id]["current_discussion"].current_message.finished_generating_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        mtdt = json.dumps(metadata, indent=4) if metadata is not None and type(metadata)== list else metadata
        if self.nb_received_tokens==1:
            run_async(
                partial(self.socketio.emit,'update_message', {
                                                "sender": self.personality.name,
                                                'id':self.connections[client_id]["current_discussion"].current_message.id, 
                                                'content': "✍ warming up ...",# self.connections[client_id]["generated_text"],
                                                'ui': ui,
                                                'discussion_id':self.connections[client_id]["current_discussion"].discussion_id,
                                                'message_type': MSG_TYPE.MSG_TYPE_STEP_END.value,
                                                'finished_generating_at': self.connections[client_id]["current_discussion"].current_message.finished_generating_at,
                                                'parameters':parameters,
                                                'metadata':metadata
                                            }, to=client_id
                                    )
            )

        run_async(
            partial(self.socketio.emit,'update_message', {
                                            "sender": self.personality.name,
                                            'id':self.connections[client_id]["current_discussion"].current_message.id, 
                                            'content': chunk,# self.connections[client_id]["generated_text"],
                                            'ui': ui,
                                            'discussion_id':self.connections[client_id]["current_discussion"].discussion_id,
                                            'message_type': msg_type.value if msg_type is not None else MSG_TYPE.MSG_TYPE_CHUNK.value if self.nb_received_tokens>1 else MSG_TYPE.MSG_TYPE_FULL.value,
                                            'finished_generating_at': self.connections[client_id]["current_discussion"].current_message.finished_generating_at,
                                            'parameters':parameters,
                                            'metadata':metadata
                                        }, to=client_id
                                )
        )
        if msg_type != MSG_TYPE.MSG_TYPE_INFO:
            self.connections[client_id]["current_discussion"].update_message(self.connections[client_id]["generated_text"], new_metadata=mtdt, new_ui=ui)



    def close_message(self, client_id):
        if not self.connections[client_id]["current_discussion"]:
            return
        #fix halucination
        self.connections[client_id]["generated_text"]=self.connections[client_id]["generated_text"].split("!@>")[0]
        # Send final message
        self.connections[client_id]["current_discussion"].current_message.finished_generating_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        run_async(
            partial(self.socketio.emit,'close_message', {
                                            "sender": self.personality.name,
                                            "id": self.connections[client_id]["current_discussion"].current_message.id,
                                            "content":self.connections[client_id]["generated_text"],

                                            'binding': self.config["binding_name"],
                                            'model' : self.config["model_name"], 
                                            'personality':self.config["personalities"][self.config["active_personality_id"]],

                                            'created_at': self.connections[client_id]["current_discussion"].current_message.created_at,
                                            'finished_generating_at': self.connections[client_id]["current_discussion"].current_message.finished_generating_at,

                                        }, to=client_id
                                )
        )

    def process_chunk(
                        self, 
                        chunk:str, 
                        message_type:MSG_TYPE,
                        parameters:dict=None, 
                        metadata:list=None, 
                        client_id:int=0,
                        personality:AIPersonality=None
                    ):
        """
        Processes a chunk of generated text
        """
        if chunk is None:
            return True
        if not client_id in list(self.connections.keys()):
            self.error("Connection lost", client_id=client_id)
            return
        if message_type == MSG_TYPE.MSG_TYPE_STEP:
            ASCIIColors.info("--> Step:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_STEP_START:
            ASCIIColors.info("--> Step started:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_STEP_END:
            if parameters['status']:
                ASCIIColors.success("--> Step ended:"+chunk)
            else:
                ASCIIColors.error("--> Step ended:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_EXCEPTION:
            self.error(chunk, client_id=client_id)
            ASCIIColors.error("--> Exception from personality:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_WARNING:
            self.warning(chunk,client_id=client_id)
            ASCIIColors.error("--> Exception from personality:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_INFO:
            self.info(chunk, client_id=client_id)
            ASCIIColors.info("--> Info:"+chunk)
        if message_type == MSG_TYPE.MSG_TYPE_UI:
            self.update_message(client_id, "", parameters, metadata, chunk, MSG_TYPE.MSG_TYPE_UI)

        if message_type == MSG_TYPE.MSG_TYPE_NEW_MESSAGE:
            self.nb_received_tokens = 0
            self.start_time = datetime.now()
            self.new_message(
                                    client_id, 
                                    self.personality.name if personality is None else personality.name, 
                                    chunk if parameters["type"]!=MSG_TYPE.MSG_TYPE_UI.value else '', 
                                    metadata = [{
                                        "title":chunk,
                                        "content":parameters["metadata"]
                                        }
                                    ] if parameters["type"]==MSG_TYPE.MSG_TYPE_JSON_INFOS.value else None, 
                                    ui= chunk if parameters["type"]==MSG_TYPE.MSG_TYPE_UI.value else None, 
                                    message_type= MSG_TYPE(parameters["type"])
            )

        elif message_type == MSG_TYPE.MSG_TYPE_FINISHED_MESSAGE:
            self.close_message(client_id)

        elif message_type == MSG_TYPE.MSG_TYPE_CHUNK:
            if self.nb_received_tokens==0:
                self.start_time = datetime.now()
            dt =(datetime.now() - self.start_time).seconds
            if dt==0:
                dt=1
            spd = self.nb_received_tokens/dt
            ASCIIColors.green(f"Received {self.nb_received_tokens} tokens (speed: {spd:.2f}t/s)              ",end="\r",flush=True) 
            sys.stdout = sys.__stdout__
            sys.stdout.flush()
            if chunk:
                self.connections[client_id]["generated_text"] += chunk
            antiprompt = self.personality.detect_antiprompt(self.connections[client_id]["generated_text"])
            if antiprompt:
                ASCIIColors.warning(f"\nDetected hallucination with antiprompt: {antiprompt}")
                self.connections[client_id]["generated_text"] = self.remove_text_from_string(self.connections[client_id]["generated_text"],antiprompt)
                self.update_message(client_id, self.connections[client_id]["generated_text"], parameters, metadata, None, MSG_TYPE.MSG_TYPE_FULL)
                return False
            else:
                self.nb_received_tokens += 1
                if self.connections[client_id]["continuing"] and self.connections[client_id]["first_chunk"]:
                    self.update_message(client_id, self.connections[client_id]["generated_text"], parameters, metadata)
                else:
                    self.update_message(client_id, chunk, parameters, metadata, msg_type=MSG_TYPE.MSG_TYPE_CHUNK)
                self.connections[client_id]["first_chunk"]=False
                # if stop generation is detected then stop
                if not self.cancel_gen:
                    return True
                else:
                    self.cancel_gen = False
                    ASCIIColors.warning("Generation canceled")
                    return False
 
        # Stream the generated text to the main process
        elif message_type == MSG_TYPE.MSG_TYPE_FULL:
            self.connections[client_id]["generated_text"] = chunk
            self.nb_received_tokens += 1
            dt =(datetime.now() - self.start_time).seconds
            if dt==0:
                dt=1
            spd = self.nb_received_tokens/dt
            ASCIIColors.green(f"Received {self.nb_received_tokens} tokens (speed: {spd:.2f}t/s)              ",end="\r",flush=True) 
            antiprompt = self.personality.detect_antiprompt(self.connections[client_id]["generated_text"])
            if antiprompt:
                ASCIIColors.warning(f"\nDetected hallucination with antiprompt: {antiprompt}")
                self.connections[client_id]["generated_text"] = self.remove_text_from_string(self.connections[client_id]["generated_text"],antiprompt)
                self.update_message(client_id, self.connections[client_id]["generated_text"], parameters, metadata, None, MSG_TYPE.MSG_TYPE_FULL)
                return False

            self.update_message(client_id, chunk,  parameters, metadata, ui=None, msg_type=message_type)
            return True
        # Stream the generated text to the frontend
        else:
            self.update_message(client_id, chunk, parameters, metadata, ui=None, msg_type=message_type)
        return True


    def generate(self, full_prompt, prompt, context_details, n_predict, client_id, callback=None):
        if self.personality.processor is not None:
            ASCIIColors.info("Running workflow")
            try:
                self.personality.callback = callback
                self.personality.processor.run_workflow( prompt, full_prompt, callback, context_details)
            except Exception as ex:
                trace_exception(ex)
                # Catch the exception and get the traceback as a list of strings
                traceback_lines = traceback.format_exception(type(ex), ex, ex.__traceback__)
                # Join the traceback lines into a single string
                traceback_text = ''.join(traceback_lines)
                ASCIIColors.error(f"Workflow run failed.\nError:{ex}")
                ASCIIColors.error(traceback_text)
                if callback:
                    callback(f"Workflow run failed\nError:{ex}", MSG_TYPE.MSG_TYPE_EXCEPTION)                   
            print("Finished executing the workflow")
            return


        self._generate(full_prompt, n_predict, client_id, callback)
        ASCIIColors.success("\nFinished executing the generation")

    def _generate(self, prompt, n_predict, client_id, callback=None):
        self.nb_received_tokens = 0
        self.start_time = datetime.now()
        if self.model is not None:
            if self.model.binding_type==BindingType.TEXT_IMAGE and len(self.personality.image_files)>0:
                ASCIIColors.info(f"warmup for generating up to {n_predict} tokens")
                if self.config["override_personality_model_parameters"]:
                    output = self.model.generate_with_images(
                        prompt,
                        self.personality.image_files,
                        callback=callback,
                        n_predict=n_predict,
                        temperature=self.config['temperature'],
                        top_k=self.config['top_k'],
                        top_p=self.config['top_p'],
                        repeat_penalty=self.config['repeat_penalty'],
                        repeat_last_n = self.config['repeat_last_n'],
                        seed=self.config['seed'],
                        n_threads=self.config['n_threads']
                    )
                else:
                    output = self.model.generate_with_images(
                        prompt,
                        self.personality.image_files,
                        callback=callback,
                        n_predict=min(n_predict,self.personality.model_n_predicts),
                        temperature=self.personality.model_temperature,
                        top_k=self.personality.model_top_k,
                        top_p=self.personality.model_top_p,
                        repeat_penalty=self.personality.model_repeat_penalty,
                        repeat_last_n = self.personality.model_repeat_last_n,
                        seed=self.config['seed'],
                        n_threads=self.config['n_threads']
                    )                
            else:
                ASCIIColors.info(f"warmup for generating up to {n_predict} tokens")
                if self.config["override_personality_model_parameters"]:
                    output = self.model.generate(
                        prompt,
                        callback=callback,
                        n_predict=n_predict,
                        temperature=self.config['temperature'],
                        top_k=self.config['top_k'],
                        top_p=self.config['top_p'],
                        repeat_penalty=self.config['repeat_penalty'],
                        repeat_last_n = self.config['repeat_last_n'],
                        seed=self.config['seed'],
                        n_threads=self.config['n_threads']
                    )
                else:
                    output = self.model.generate(
                        prompt,
                        callback=callback,
                        n_predict=min(n_predict,self.personality.model_n_predicts),
                        temperature=self.personality.model_temperature,
                        top_k=self.personality.model_top_k,
                        top_p=self.personality.model_top_p,
                        repeat_penalty=self.personality.model_repeat_penalty,
                        repeat_last_n = self.personality.model_repeat_last_n,
                        seed=self.config['seed'],
                        n_threads=self.config['n_threads']
                    )
        else:
            print("No model is installed or selected. Please make sure to install a model and select it inside your configuration before attempting to communicate with the model.")
            print("To do this: Install the model to your models/<binding name> folder.")
            print("Then set your model information in your local configuration file that you can find in configs/local_config.yaml")
            print("You can also use the ui to set your model in the settings page.")
            output = ""
        return output

    def start_message_generation(self, message, message_id, client_id, is_continue=False, generation_type=None):
        if self.personality is None:
            self.warning("Select a personality")
            return
        ASCIIColors.info(f"Text generation requested by client: {client_id}")
        # send the message to the bot
        print(f"Received message : {message.content}")
        if self.connections[client_id]["current_discussion"]:
            if not self.model:
                self.error("No model selected. Please make sure you select a model before starting generation", client_id=client_id)
                return          
            # First we need to send the new message ID to the client
            if is_continue:
                self.connections[client_id]["current_discussion"].load_message(message_id)
                self.connections[client_id]["generated_text"] = message.content
            else:
                self.new_message(client_id, self.personality.name, "")
                self.update_message(client_id, "✍ warming up ...", msg_type=MSG_TYPE.MSG_TYPE_STEP_START)
            self.socketio.sleep(0.01)

            # prepare query and reception
            self.discussion_messages, self.current_message, tokens, context_details = self.prepare_query(client_id, message_id, is_continue, n_tokens=self.config.min_n_predict, generation_type=generation_type)
            self.prepare_reception(client_id)
            self.generating = True
            self.connections[client_id]["processing"]=True
            try:
                self.generate(
                                self.discussion_messages, 
                                self.current_message,
                                context_details=context_details,
                                n_predict = self.config.ctx_size-len(tokens)-1,
                                client_id=client_id,
                                callback=partial(self.process_chunk,client_id = client_id)
                            )
                if self.config.enable_voice_service and self.config.auto_read and len(self.personality.audio_samples)>0:
                    try:
                        self.process_chunk("Generating voice output",MSG_TYPE.MSG_TYPE_STEP_START,client_id=client_id)
                        from lollms.services.xtts.lollms_xtts import LollmsXTTS
                        if self.tts is None:
                            self.tts = LollmsXTTS(self, voice_samples_path=Path(__file__).parent.parent/"voices")
                        language = convert_language_name(self.personality.language)
                        self.tts.set_speaker_folder(Path(self.personality.audio_samples[0]).parent)
                        fn = self.personality.name.lower().replace(' ',"_").replace('.','')    
                        fn = f"{fn}_{message_id}.wav"
                        url = f"audio/{fn}"
                        self.tts.tts_to_file(self.connections[client_id]["generated_text"], Path(self.personality.audio_samples[0]).name, f"{fn}", language=language)
                        fl = f"""
<audio controls>
    <source src="{url}" type="audio/wav">
    Your browser does not support the audio element.
</audio>
"""
                        self.process_chunk("Generating voice output", MSG_TYPE.MSG_TYPE_STEP_END, {'status':True},client_id=client_id)
                        self.process_chunk(fl,MSG_TYPE.MSG_TYPE_UI, client_id=client_id)
                        
                        """
                        self.info("Creating audio output",10)
                        self.personality.step_start("Creating audio output")
                        if not PackageManager.check_package_installed("tortoise"):
                            PackageManager.install_package("tortoise-tts")
                        from tortoise import utils, api
                        import sounddevice as sd
                        if self.tts is None:
                            self.tts = api.TextToSpeech( kv_cache=True, half=True)
                        reference_clips = [utils.audio.load_audio(str(p), 22050) for p in self.personality.audio_samples]
                        tk = self.model.tokenize(self.connections[client_id]["generated_text"])
                        if len(tk)>100:
                            chunk_size = 100
                            
                            for i in range(0, len(tk), chunk_size):
                                chunk = self.model.detokenize(tk[i:i+chunk_size])
                                if i==0:
                                    pcm_audio = self.tts.tts_with_preset(chunk, voice_samples=reference_clips, preset='fast').numpy().flatten()
                                else:
                                    pcm_audio = np.concatenate([pcm_audio, self.tts.tts_with_preset(chunk, voice_samples=reference_clips, preset='ultra_fast').numpy().flatten()])
                        else:
                            pcm_audio = self.tts.tts_with_preset(self.connections[client_id]["generated_text"], voice_samples=reference_clips, preset='fast').numpy().flatten()
                        sd.play(pcm_audio, 22050)
                        self.personality.step_end("Creating audio output")                        
                        """



                    except Exception as ex:
                        ASCIIColors.error("Couldn't read")
                        trace_exception(ex)
                print()
                ASCIIColors.success("## Done Generation ##")
                print()
            except Exception as ex:
                trace_exception(ex)
                print()
                ASCIIColors.error("## Generation Error ##")
                print()

            self.cancel_gen = False

            # Send final message
            self.close_message(client_id)
            self.connections[client_id]["processing"]=False
            if self.connections[client_id]["schedule_for_deletion"]:
                del self.connections[client_id]

            ASCIIColors.success(f" ╔══════════════════════════════════════════════════╗ ")
            ASCIIColors.success(f" ║                        Done                      ║ ")
            ASCIIColors.success(f" ╚══════════════════════════════════════════════════╝ ")
            if self.config.auto_title:
                d = self.connections[client_id]["current_discussion"]
                ttl = d.title()
                if ttl is None or ttl=="" or ttl=="untitled":
                    title = self.make_discussion_title(d, client_id=client_id)
                    d.rename(title)
                    asyncio.run(
                        self.socketio.emit('disucssion_renamed',{
                                                    'status': True,
                                                    'discussion_id':d.discussion_id,
                                                    'title':title
                                                    }, to=client_id) 
                    )
            self.busy=False

        else:
            ump = self.config.discussion_prompt_separator +self.config.user_name.strip() if self.config.use_user_name_in_discussions else self.personality.user_message_prefix
            
            self.cancel_gen = False
            #No discussion available
            ASCIIColors.warning("No discussion selected!!!")

            self.error("No discussion selected!!!", client_id=client_id)
            
            print()
            self.busy=False
            return ""