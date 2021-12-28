import chunk
import subchunk
import ctypes
import math
import logging
import glm



from collections import deque

import pyglet.gl as gl

import block_type
import models
import save
import options
from util import DIRECTIONS

class Queue:
	def __init__(self):
		self.queue = deque()
	
	def put_nowait(self, item):
		self.queue.append(item)

	def get_nowait(self):
		return self.queue.popleft()
	
	def qsize(self):
		return len(self.queue)


def get_chunk_position(position):
	x, y, z = position

	return glm.ivec3(
		(x // chunk.CHUNK_WIDTH),
		(y // chunk.CHUNK_HEIGHT),
		(z // chunk.CHUNK_LENGTH))


def get_local_position(position):
	x, y, z = position
	
	return glm.ivec3(
		int(x % chunk.CHUNK_WIDTH),
		int(y % chunk.CHUNK_HEIGHT),
		int(z % chunk.CHUNK_LENGTH))


class World:
	def __init__(self, shader, camera, texture_manager):
		self.shader = shader
		self.camera = camera
		self.texture_manager = texture_manager
		self.block_types = [None]

		self.shader_daylight_location = shader.find_uniform(b"u_Daylight")
		self.daylight = 1800
		self.incrementer = 0
		self.time = 0

		# Compat
		self.get_chunk_position = get_chunk_position
		self.get_local_position = get_local_position

		# parse block type data file

		blocks_data_file = open("data/blocks.mcpy")
		blocks_data = blocks_data_file.readlines()
		blocks_data_file.close()

		logging.info("Loading block models")
		for block in blocks_data:
			if block[0] in ['\n', '#']: # skip if empty line or comment
				continue
			
			number, props = block.split(':', 1)
			number = int(number)

			# default block

			name = "Unknown"
			model = models.cube
			texture = {"all": "unknown"}

			# read properties

			for prop in props.split(','):
				prop = prop.strip()
				prop = list(filter(None, prop.split(' ', 1)))

				if prop[0] == "sameas":
					sameas_number = int(prop[1])

					name = self.block_types[sameas_number].name
					texture = self.block_types[sameas_number].block_face_textures
					model = self.block_types[sameas_number].model
				
				elif prop[0] == "name":
					name = eval(prop[1])
				
				elif prop[0][:7] == "texture":
					_, side = prop[0].split('.')
					texture[side] = prop[1].strip()

				elif prop[0] == "model":
					model = eval(prop[1])
			
			# add block type

			_block_type = block_type.Block_type(self.texture_manager, name, texture, model)

			if number < len(self.block_types):
				self.block_types[number] = _block_type
			
			else:
				self.block_types.append(_block_type)

		self.light_blocks = [10, 11, 50, 51, 62, 75]

		self.texture_manager.generate_mipmaps()

		indices = []

		for nquad in range(chunk.CHUNK_WIDTH * chunk.CHUNK_HEIGHT * chunk.CHUNK_LENGTH * 8):
			indices.append(4 * nquad + 0)
			indices.append(4 * nquad + 1)
			indices.append(4 * nquad + 2)
			indices.append(4 * nquad + 0)
			indices.append(4 * nquad + 2)
			indices.append(4 * nquad + 3)


		self.ibo = gl.GLuint(0)
		gl.glGenBuffers(1, self.ibo)
		gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self.ibo)
		gl.glBufferData(
			gl.GL_ELEMENT_ARRAY_BUFFER,
			ctypes.sizeof(gl.GLuint * len(indices)),
			(gl.GLuint * len(indices))(*indices),
			gl.GL_STATIC_DRAW)
		gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, 0)

		logging.debug("Created Shared Index Buffer")

		# load the world

		self.save = save.Save(self)

		self.chunks = {}

		# light update queue

		self.light_increase_queue = Queue() # Node: World Position, light
		self.light_decrease_queue = Queue() # Node: World position, light
		self.skylight_increase_queue = Queue()
		self.skylight_decrease_queue = Queue()
		self.chunk_update_queue = Queue() 

		self.save.load()
		
		logging.info("Lighting chunks")
		for world_chunk in self.chunks.values():
			self.init_skylight(world_chunk)

		logging.info("Generating chunks")
		for world_chunk in self.chunks.values():
			world_chunk.update_subchunk_meshes()
			world_chunk.update_mesh()

		del indices

	def __del__(self):
		gl.glDeleteBuffers(1, ctypes.byref(self.ibo))

	def push_light_update(self, light_update, chunk, local_pos):
		lx, ly, lz = local_pos
		if light_update:
			sx = lx // subchunk.SUBCHUNK_WIDTH
			sy = ly // subchunk.SUBCHUNK_HEIGHT
			sz = lz // subchunk.SUBCHUNK_LENGTH

			if (chunk, sx, sy, sz) not in self.chunk_update_queue.queue:
				self.chunk_update_queue.put_nowait((chunk, sx, sy, sz))

	def increase_light(self, world_pos, newlight, light_update=True):
		chunk = self.chunks[get_chunk_position(world_pos)]
		local_pos = get_local_position(world_pos)

		chunk.set_block_light(local_pos, newlight)

		self.light_increase_queue.put_nowait((world_pos, newlight))

		self.propagate_increase(light_update)

	def propagate_increase(self, light_update):
		while self.light_increase_queue.qsize():
			pos, light_level = self.light_increase_queue.get_nowait()

			for direction in DIRECTIONS:
				neighbour_pos = pos + direction

				chunk = self.chunks.get(get_chunk_position(neighbour_pos), None)
				if not chunk: continue
				local_pos = get_local_position(neighbour_pos)

				if not self.is_opaque_block(neighbour_pos) and chunk.get_block_light(local_pos) + 2 <= light_level:
					chunk.set_block_light(local_pos, light_level - 1)

					self.light_increase_queue.put_nowait((neighbour_pos, light_level - 1))

					self.push_light_update(light_update, chunk, local_pos)

	def init_skylight(self, pending_chunk):
		for lx in range(chunk.CHUNK_WIDTH):
			for lz in range(chunk.CHUNK_LENGTH):
				pending_chunk.set_sky_light(glm.ivec3(lx, chunk.CHUNK_HEIGHT-1, lz), 15)

				chunk_pos = pending_chunk.chunk_position
				pos = glm.ivec3(chunk.CHUNK_WIDTH * chunk_pos[0] + lx,
						chunk.CHUNK_HEIGHT - 1,
						chunk.CHUNK_LENGTH * chunk_pos[2] + lz
				)

				self.skylight_increase_queue.put_nowait((pos, 16))

		self.propagate_skylight_increase(False)

	def propagate_skylight_increase(self, light_update):
		while self.skylight_increase_queue.qsize():
			pos, light_level = self.skylight_increase_queue.get_nowait()

			for direction in DIRECTIONS:
				_, dy, _ = direction
				if dy > 0: continue

				neighbour_pos = pos + direction
				chunk = self.chunks.get(get_chunk_position(neighbour_pos), None)
				if not chunk: continue
				local_pos = get_local_position(neighbour_pos)

				if not self.is_opaque_block(neighbour_pos) and chunk.get_sky_light(local_pos) + 2 <= light_level:
					chunk.set_sky_light(local_pos, light_level - 1)
					if not dy:
						self.skylight_increase_queue.put_nowait((neighbour_pos, light_level - 1))
					else:
						self.skylight_increase_queue.put_nowait((neighbour_pos, light_level))

					self.push_light_update(light_update, chunk, local_pos)

	def decrease_light(self, world_pos):
		chunk = self.chunks[get_chunk_position(world_pos)]
		local_pos = get_local_position(world_pos)
		old_light = chunk.get_block_light(local_pos)
		chunk.set_block_light(local_pos, 0)
		self.light_decrease_queue.put_nowait((world_pos, old_light))
		
		self.propagate_decrease(True)
		self.propagate_increase(True)

	def propagate_decrease(self, light_update):
		while self.light_decrease_queue.qsize():
			pos, light_level = self.light_decrease_queue.get_nowait()

			for direction in DIRECTIONS:
				neighbour_pos = pos + direction

				chunk = self.chunks.get(get_chunk_position(neighbour_pos), None)
				if not chunk: continue
				local_pos = get_local_position(neighbour_pos)

				if not self.is_opaque_block(neighbour_pos):
					neighbour_level = chunk.get_block_light(local_pos)
					if not neighbour_level: continue

					if neighbour_level < light_level:
						chunk.set_block_light(local_pos, 0)
						self.light_decrease_queue.put_nowait((neighbour_pos, neighbour_level))
					elif neighbour_level >= light_level:
						self.light_increase_queue.put_nowait((neighbour_pos, neighbour_level))

					self.push_light_update(light_update, chunk, local_pos)
	
	def decrease_skylight(self, world_pos, light_update=True):
		chunk = self.chunks[get_chunk_position(world_pos)]
		local_pos = get_local_position(world_pos)
		old_light = chunk.get_sky_light(local_pos)
		chunk.set_sky_light(local_pos, 0)
		self.skylight_decrease_queue.put_nowait((world_pos, old_light))
		
		self.propagate_skylight_decrease(light_update)
		self.propagate_skylight_increase(light_update)

	def propagate_skylight_decrease(self, light_update=True):
		while self.skylight_decrease_queue.qsize():
			pos, light_level = self.skylight_decrease_queue.get_nowait()

			for direction in DIRECTIONS:
				dy = direction.y
				neighbour_pos = pos + direction

				chunk = self.chunks.get(get_chunk_position(neighbour_pos), None)
				if not chunk: continue
				local_pos = get_local_position(neighbour_pos)
				
				if not self.is_opaque_block(neighbour_pos):
					neighbour_level = chunk.get_sky_light(local_pos)
					if not neighbour_level: continue

					if neighbour_level < light_level or (dy == -1 and neighbour_level == 15):
						chunk.set_sky_light(local_pos, 0)
						self.skylight_decrease_queue.put_nowait((neighbour_pos, neighbour_level))
					elif neighbour_level >= light_level:
						self.skylight_increase_queue.put_nowait((neighbour_pos, neighbour_level))

					self.push_light_update(light_update, chunk, local_pos)

	def get_light(self, position):
		chunk = self.chunks.get(get_chunk_position(position), None)
		if not chunk:
			return 0
		local_position = self.get_local_position(position)
		return chunk.get_block_light(local_position)
	
	def get_skylight(self, position):
		chunk = self.chunks.get(get_chunk_position(position), None)
		if not chunk:
			return 0
		local_position = self.get_local_position(position)
		return chunk.get_sky_light(local_position)

	def set_light(self, position, light):
		chunk = self.chunks.get(get_chunk_position(position), None)
		local_position = get_local_position(position)
		chunk.set_block_light(local_position, light)

	def set_skylight(self, position, light):
		chunk = self.chunks.get(get_chunk_position(position), None)
		local_position = get_local_position(position)
		chunk.set_sky_light(local_position, light)


	def get_block_number(self, position):
		chunk_position = get_chunk_position(position)

		if not chunk_position in self.chunks:
			return 0
		
		lx, ly, lz = get_local_position(position)

		block_number = self.chunks[chunk_position].blocks[lx][ly][lz]
		return block_number

	def is_opaque_block(self, position):
		# get block type and check if it's opaque or not
		# air counts as a transparent block, so test for that too
		
		block_type = self.block_types[self.get_block_number(position)]

		if not block_type:
			return False
		
		return not block_type.transparent

	def set_block(self, position, number): # set number to 0 (air) to remove block
		x, y, z = position
		chunk_position = get_chunk_position(position)

		if not chunk_position in self.chunks: # if no chunks exist at this position, create a new one
			if number == 0:
				return # no point in creating a whole new chunk if we're not gonna be adding anything

			self.chunks[chunk_position] = chunk.Chunk(self, chunk_position)
		
		if self.get_block_number(position) == number: # no point updating mesh if the block is the same
			return
		
		lx, ly, lz = get_local_position(position)

		self.chunks[chunk_position].blocks[lx][ly][lz] = number
		self.chunks[chunk_position].modified = True

		if number:
			if number in self.light_blocks:
				self.increase_light(position, 15)

			elif not self.block_types[number].transparent:
				self.decrease_light(position)
				self.decrease_skylight(position)
		
		elif not number:
			self.decrease_light(position)
			self.decrease_skylight(position)

		self.chunks[chunk_position].update_at_position((x, y, z))
		self.chunks[chunk_position].update_mesh()

		cx, cy, cz = chunk_position

		def try_update_chunk_at_position(chunk_position, position):
			if chunk_position in self.chunks:
				self.chunks[chunk_position].update_at_position(position)
				self.chunks[chunk_position].update_mesh()
		
		if lx == chunk.CHUNK_WIDTH - 1: try_update_chunk_at_position(glm.ivec3(cx + 1, cy, cz), (x + 1, y, z))
		if lx == 0: try_update_chunk_at_position(glm.ivec3(cx - 1, cy, cz), (x - 1, y, z))

		if ly == chunk.CHUNK_HEIGHT - 1: try_update_chunk_at_position(glm.ivec3(cx, cy + 1, cz), (x, y + 1, z))
		if ly == 0: try_update_chunk_at_position(glm.ivec3(cx, cy - 1, cz), (x, y - 1, z))

		if lz == chunk.CHUNK_LENGTH - 1: try_update_chunk_at_position(glm.ivec3(cx, cy, cz + 1), (x, y, z + 1))
		if lz == 0: try_update_chunk_at_position(glm.ivec3(cx, cy, cz - 1), (x, y, z - 1))

	def update_time(self, delta_time):
		self.time += 1
	
	def speed_daytime(self):
		if self.daylight <= 0:
			self.incrementer = 1
		if self.daylight >= 1800:
			self.incrementer = -1
	
	def can_render_chunk(self, chunk_position, pl_c_pos):
		rx, ry, rz = (chunk_position[0] - pl_c_pos[0]) \
					* math.cos(self.camera.rotation[0]) \
					* math.cos(self.camera.rotation[1]) , \
				(chunk_position[1] - pl_c_pos[1]) \
					* math.sin(self.camera.rotation[1]) , \
				(chunk_position[2] - pl_c_pos[2]) \
					* math.sin(self.camera.rotation[0]) \
					* math.cos(self.camera.rotation[1])
		return rx >= -1 and ry >= -1 and rz >= -1 
	
	def draw_translucent_fast(self, player_chunk_pos):
		gl.glDisable(gl.GL_CULL_FACE)
		gl.glEnable(gl.GL_BLEND)
		gl.glDepthMask(gl.GL_FALSE)

		for chunk_position, render_chunk in self.chunks.items():
			if self.can_render_chunk(chunk_position, player_chunk_pos):
				render_chunk.draw_translucent()

		gl.glDepthMask(gl.GL_TRUE)
		gl.glDisable(gl.GL_BLEND)
		gl.glEnable(gl.GL_CULL_FACE)
		
	def draw_translucent_fancy(self, player_chunk_pos):
		gl.glDepthMask(gl.GL_FALSE)
		gl.glFrontFace(gl.GL_CW)
		gl.glEnable(gl.GL_BLEND)

		for chunk_position, render_chunk in self.chunks.items():
			if self.can_render_chunk(chunk_position, player_chunk_pos):
				render_chunk.draw_translucent()
		
		gl.glFrontFace(gl.GL_CCW)
		
		for chunk_position, render_chunk in self.chunks.items():
			if self.can_render_chunk(chunk_position, player_chunk_pos):
				render_chunk.draw_translucent()

		gl.glDisable(gl.GL_BLEND)
		gl.glDepthMask(gl.GL_TRUE)

	draw_translucent = draw_translucent_fancy if options.TRANSLUCENT_BLENDING else draw_translucent_fast
	
	def draw(self):
		daylight_multiplier = self.daylight / 1800
		gl.glClearColor(0.4 * daylight_multiplier, 0.7 * daylight_multiplier, daylight_multiplier, 1.0)
		gl.glUniform1f(self.shader_daylight_location, daylight_multiplier)

		player_floored_pos = tuple(self.camera.position)
		player_chunk_pos = self.get_chunk_position(player_floored_pos)

		for chunk_position, render_chunk in self.chunks.items():
			if self.can_render_chunk(chunk_position, player_chunk_pos):
				render_chunk.draw()

		gl.glUniform1f(self.shader_daylight_location, 0.75 + daylight_multiplier / 4)
		self.draw_translucent(player_chunk_pos)

	def tick(self):
		if self.incrementer == -1:
			if self.daylight < 0:
				self.incrementer = 0
		elif self.incrementer == 1:
			if self.daylight >= 1800:
				self.incrementer = 0

		if self.time % 600 == 0:
			self.incrementer = 1
		elif self.time % 600 == 300:
			self.incrementer = -1

		self.daylight += self.incrementer

	def update(self):
		if self.chunk_update_queue.qsize():
			pending_chunk, sx, sy, sz = self.chunk_update_queue.get_nowait()
			pending_chunk.subchunks[(sx, sy, sz)].update_mesh()
			pending_chunk.update_mesh()
				
		
	
		
